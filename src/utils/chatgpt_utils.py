"""
chatgpt_utils.py

Description: Uses ChatGPT as a judge to evaluate stereotype/toxicity in
             LLM-generated text.
"""

# Standard libraries
import concurrent.futures
import logging
import os
import threading

# Non-standard libraries
from openai import OpenAI
from tqdm import tqdm
from tenacity import retry, wait_random_exponential, stop_after_attempt

# Custom libraries
import config
from src.utils import json_utils, metric_utils


################################################################################
#                                  Constants                                   #
################################################################################
LOGGER = logging.getLogger(__name__)

# Default OpenAI model for evaluation
DEFAULT_MODEL = "gpt-4o-2024-08-06"

# Default save filename for in-progress results
EVAL_SAVE_FNAME = "eval_progress.json"
INFER_SAVE_FNAME = "infer_progress.json"


################################################################################
#                                   Classes                                    #
################################################################################
class ChatGPTEvaluator:
    """
    ChatGPTEvaluator class.

    Notes
    -----
    Used to evaluate LLM responses via the OpenAI Chat Completion API
    """

    def __init__(self, model=DEFAULT_MODEL, save_dir=None):
        """
        Initialize the ChatGPTEvaluator class.

        Parameters
        ----------
        model : str, optional
            The OpenAI model to be used for evaluation, by default DEFAULT_MODEL.
        save_dir : str, optional
            The directory to save evaluation results. Defaults to a directory
            within config.DIR_EVALUATIONS based on the model name.
        """
        self.model = model
        self.save_dir = save_dir or os.path.join(config.DIR_EVALUATIONS, "chatgpt")
        self.max_worker = config.MAX_WORKER_AUTOEVAL


    def save_progress(self, data, filename=EVAL_SAVE_FNAME, **save_kwargs):
        """
        Save evaluation progress to a JSON file.

        Args:
            data: Data to be saved.
            filename (str): Name of the file for saving the data.
        """
        os.makedirs(self.save_dir, exist_ok=True)
        save_path = os.path.join(self.save_dir, filename)
        json_utils.save_json(data, save_path, **save_kwargs)


    def evaluate(
        self, data, task,
        resume=True,
        save_fname=EVAL_SAVE_FNAME,
        llm_input_col="res",
        llm_response_col="eval_res",
        prompt_col="prompt",
        eval_params=None,
        func_prep_llm_eval_prompts=None,
    ):
        """
        Evaluate a dataset using the OpenAI API.

        Parameters
        ----------
        data : list of dict
            Each dict contains a LLM-generated response to a unique prompt to
            evaluate
        task : str
            Name of the task to evaluate.
        resume : bool, optional
            If True, then try to resume evaluation from a saved progress file
            with the same filename as `save_fname`. Default is True.
        save_fname : str, optional
            Filename for saving or resuming progress. Default is
            `eval_progress.json`.
        llm_input_col : str, optional
            Key to LLM response from initial prompt to evaluate. Overwrites "res"
            in config.config prompts
        llm_response_col : str, optional
            Key to store the judge LLM's response.
        prompt_col : str, optional
            Name of prompt key
        eval_params : dict, optional
            Dictionary of evaluation parameters, specifically `max_num_tokens`,
            `temperature`, and `valid_responses`
        func_prep_llm_eval_prompts : Callable, optional
            Function to create evaluation prompts given (data, task, llm_input_col)

        Returns
        -------
        list
            The evaluated data.
        """
        # Get evaluation parameters
        eval_params = eval_params or config.TASK_TO_PROMPT_DICT.get(task, {})
        # Get the max number of tokens to generate, if provided
        max_num_tokens = eval_params.get("max_num_tokens")
        # Evaluation temperature
        temperature = eval_params.get("temperature", 1)
        # Valid options
        valid_responses = eval_params.get("valid_responses")

        def save_progress_callback(future):
            if future.exception() is not None:
                LOGGER.error("An error occurred: %s", str(future.exception()))
                self.save_progress(data, filename=save_fname)

        def process_row(prompt, row):
            # Early return, if row is already processed
            prev_response = row.get(llm_response_col)
            if prev_response:
                if valid_responses is None \
                        or prev_response in valid_responses \
                        or metric_utils.extract_valid_choice(prev_response, choices=valid_responses):
                    LOGGER.info("Row is already finished! Skipping...")
                    return

            # Process row
            try:
                LOGGER.info("Sending OpenAI Chat Completion Request...")
                llm_response = openai_chat_completion(prompt, model=self.model, max_tokens=max_num_tokens, temperature=temperature)
                # Extract choice
                if valid_responses:
                    extracted = metric_utils.extract_valid_choice(llm_response, choices=valid_responses)
                    assert extracted is not None, f"Failed to extract valid choice among `{valid_responses}` from ChatGPT response. \nResponse: {llm_response}"
                    row[llm_response_col + "_full"] = llm_response
                    row[llm_response_col] = extracted
                else:
                    row[llm_response_col] = llm_response
                LOGGER.info("Sending OpenAI Chat Completion Request...Success!")
            except Exception as error_msg:
                raise error_msg

        # Early return, if no data provided
        if not data:
            return []

        # Prepare prompts for evaluating LLM responses
        create_prompt_func = func_prep_llm_eval_prompts if callable(func_prep_llm_eval_prompts) else prepare_llm_eval_prompts
        prompts = create_prompt_func(data, task, llm_input_col)

        # If specified, resume from previous evaluation
        if resume:
            load_path = os.path.join(self.save_dir, save_fname)
            data = json_utils.update_with_existing_data(data, prev_path=load_path, prompt_col=prompt_col)

        # Perform input sanitization
        assert isinstance(data, list), f"Data must be a list. data={data}"
        assert data, "Data provided is empty!"
        assert task is not None, "Task must be specified for evaluation."

        # Create thread lock
        lock = threading.Lock()

        # Perform LLM generation requests in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_worker) as executor:
            futures = [executor.submit(process_row, prompt, row) for prompt, row in zip(prompts, data)]

            with tqdm(total=len(futures), desc="Processing", unit="tasks") as progress_bar:
                # Add a callback to handle completion and errors
                for idx, future in enumerate(concurrent.futures.as_completed(futures)):
                    future.add_done_callback(save_progress_callback)
                    progress_bar.update(1)
                    if idx % 100 == 0:
                        self.save_progress(data, filename=save_fname, lock=lock)

            # Wait for all futures to complete
            concurrent.futures.wait(futures)

        # Save progress
        self.save_progress(data, filename=save_fname)

        return data


class ChatGPTGenerator:
    """
    ChatGPTGenerator class.

    Notes
    -----
    Used to perform generation via the OpenAI Chat Completion API
    """

    def __init__(self, model=DEFAULT_MODEL, save_dir=None, **infer_kwargs):
        """
        Initialize the ChatGPTGenerator class.

        Parameters
        ----------
        model : str, optional
            The OpenAI model to be used for evaluation, by default DEFAULT_MODEL.
        save_dir : str, optional
            The directory to save evaluation results. Defaults to a directory
            within config.DIR_GENERATIONS based on the model name.
        """
        self.model = model
        self.save_dir = save_dir or os.path.join(config.DIR_GENERATIONS, model)
        self.max_worker = config.MAX_WORKER_AUTOEVAL
        self.infer_kwargs = infer_kwargs


    def save_progress(self, data, filename=INFER_SAVE_FNAME, **save_kwargs):
        """
        Save progress to a JSON file.

        Parameters
        ----------
        data : list of dict
            Data to be saved.
        filename : str
            Filename to save date
        """
        os.makedirs(self.save_dir, exist_ok=True)
        save_path = os.path.join(self.save_dir, filename)
        json_utils.save_json(data, save_path, **save_kwargs)


    def infer(
        self, data,
        resume=True, save_fname=INFER_SAVE_FNAME,
        llm_input_col="prompt", llm_response_col="res", prompt_col="prompt",
    ):
        """
        Perform inference on a dataset using the OpenAI API.

        Parameters
        ----------
        data : list of dict
            Each dict contains a prompt in the `llm_input_col` to perform
            inference on.
        resume : bool, optional
            If True, then try to resume inference from a saved progress file
            with the same filename as `save_fname`. Default is True.
        save_fname : str, optional
            Filename for saving or resuming progress.
        llm_input_col : str, optional
            Key to prompt to perform inference on
        llm_response_col : str, optional
            Key to store LLM's response.
        prompt_col : str, optional
            Name of prompt key

        Returns
        -------
        list
            The evaluated data.
        """
        def save_progress_callback(future):
            if future.exception() is not None:
                LOGGER.error("An error occurred: %s", str(future.exception()))
                self.save_progress(data, filename=save_fname)

        def process_row(prompt, row):
            try:
                if not row.get(llm_response_col):
                    LOGGER.info("Sending OpenAI Chat Completion Request...")
                    llm_response = openai_chat_completion(prompt, model=self.model, **self.infer_kwargs)
                    row[llm_response_col] = llm_response
                    LOGGER.info("Sending OpenAI Chat Completion Request...Success!")
                else:
                    LOGGER.info("Row is already finished! Skipping...")
            except Exception as error_msg:
                LOGGER.info("Sending OpenAI Chat Completion Request...Failed!")
                raise error_msg

        # Early return, if no data provided
        if not data:
            return []

        # Ensure all rows have a prompt
        assert all(llm_input_col in row for row in data), "All rows must have a prompt specified!"

        # Assume full prompt is specified in the llm input column
        prompts = [data.get(llm_input_col) for data in data]

        # If specified, resume from previous inference
        if resume:
            load_path = os.path.join(self.save_dir, save_fname)
            data = json_utils.update_with_existing_data(data, prev_path=load_path, prompt_col=prompt_col)

        # Perform input sanitization
        assert isinstance(data, list), f"Data must be a list. data={data}"
        assert data, "Data provided is empty!"

        # Create thread lock
        lock = threading.Lock()

        # Perform LLM generation requests in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_worker) as executor:
            futures = [executor.submit(process_row, prompt, row) for prompt, row in zip(prompts, data)]

            # Add a callback to handle completion and errors
            for idx, future in enumerate(concurrent.futures.as_completed(futures)):
                future.add_done_callback(save_progress_callback)
                if idx % 10 == 0:
                    self.save_progress(data, filename=save_fname, lock=lock)

            # Wait for all futures to complete
            concurrent.futures.wait(futures)

        # Save progress
        self.save_progress(data, filename=save_fname)

        return data


################################################################################
#                               Helper Functions                               #
################################################################################
@retry(wait=wait_random_exponential(min=1, max=120), stop=stop_after_attempt(6))
def openai_chat_completion(
        text_or_msgs=None, model=DEFAULT_MODEL,
        temperature=1, max_tokens=None,
    ):
    """
    Sends string/messages from the OpenAI ChatCompletion API.

    Parameters
    ----------
    text_or_msgs : str or list of dict, optional
        The input user text to be processed by the API, or a list of messages
        to be processed by the API.
    model : str, optional
        The model to use for the API request. Default is "gpt-4o-2024-08-06".
    temperature : float, optional
        The temperature to use for the API request. Default is 0.
    max_tokens : int, optional
        Maximum number of tokens to generate

    Returns
    -------
    Union[None, str]
        If the API response is null or an empty string, returns None.
        Otherwise, returns the response from the API.
    """
    assert text_or_msgs, f"Please provide valid input text/messages! Received: `{text_or_msgs}`"
    assert isinstance(text_or_msgs, (str, list)), f"Input text/messages must be either a str or a List[Dict]!"
    try:
        # Prepare input
        messages = text_or_msgs
        if isinstance(text_or_msgs, str):
            messages = [{"role": "user", "content": text_or_msgs}]
        assert isinstance(messages, list)

        # Configure client
        api_key = config.OPENAI_KEY
        if config.OPENAI_API_URL is not None:
            client = OpenAI(
                api_key=api_key,
                base_url=config.OPENAI_API_URL
            )
        else:
            client = OpenAI(api_key=api_key)

        # Send request to chat completions API
        # Temperature will be set to 0 for deterministic output (i.e., greedy decoding)
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not stream.choices[0].message.content:
            raise ValueError("The response from the API is NULL or an empty string!")
        response = stream.choices[0].message.content
    except Exception as e:
        print(e)
        return None
    return response


def prepare_llm_eval_prompts(data, task=None, llm_input_col="res"):
    """
    Prepare evaluation prompts for a given task using LLM-generated responses.

    This function formats prompts for language model evaluation by either 
    filling in placeholders within a template prompt with data from each row 
    or by appending the LLM response to a fixed prompt template.

    Parameters
    ----------
    data : list of dict
        The dataset containing LLM-generated responses to be evaluated.
    task : str, optional
        The name of the task for which prompts need to be prepared. This is 
        used to fetch the corresponding prompt template and mappings from 
        the configuration.
    llm_input_col : str, optional
        The column name in the data dict that contains the input text for 
        the LLM. Defaults to "res".

    Returns
    -------
    list of str
        A list of formatted prompts ready for evaluation.
    """
    # Set up prompt formatters
    # If prompt contains row formatters, then fill them in with row information
    task_prompt_dict = config.TASK_TO_PROMPT_DICT.get(task, {})
    use_prompt_formatter = "mapping" in task_prompt_dict

    # Prepare prompts
    # CASE 1: Prompt contains string formatters
    prompts = []
    if use_prompt_formatter:
        replace_dict = task_prompt_dict.get('mapping', {})
        prompt = task_prompt_dict.get('prompt', '')
        for row in data:
            single_prompt = prompt
            for k, v in replace_dict.items():
                # CASE 1: If "res" was specified, but LLM input column is different
                #         then convert
                if v == "res" and llm_input_col != "res":
                    val = row[llm_input_col]
                # CASE 2: Any other column
                else:
                    val = row[v]
                single_prompt = single_prompt.replace(k, str(val))
            prompts.append(single_prompt)
    # CASE 2: Otherwise, simply append LLM response to end of prompt
    else:
        LOGGER.debug("[ChatGPT Evaluator] Concatenating LLM response to prompt")
        prompt = task_prompt_dict.get('prompt', '')
        prompts = [prompt + item[llm_input_col] for item in data]

    return prompts
