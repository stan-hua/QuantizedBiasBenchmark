"""
eval_utils.py

Description: Contains utility functions useful during stereotype/toxicity
             evaluation
"""

# Standard libraries
import ast
import logging
import os

# Custom libraries
from config import DEFAULT_SCORE_KEY
from src.utils import metric_utils, json_utils


################################################################################
#                                  Constants                                   #
################################################################################
LOGGER = logging.getLogger(__name__)

# Prometheus prompt version
JUDGE_PROMPT_VER = int(os.environ.get("JUDGE_PROMPT_VER", "4"))


################################################################################
#                              Evaluation Helpers                              #
################################################################################
def score_open_ended_responses(
        data, evaluator, evaluator_choice,
        bias_type="stereotype", instruction_type="continuation",
        resume_from=None,
        filter_kwargs=None,
        evaluate_func=None, eval_func_kwargs=None, 
        **metric_kwargs,
    ):
    """
    Computes bias score for a given set of open-ended responses.

    Parameters
    ----------
    data : list of dict
        Each dict contains a LLM-generated response to a unique prompt for
        generating a stereotype-consistent or inconsistent continuation.
        The dict must contain a 'res' key with the response text.
    evaluator : Evaluator
        An instance of an Evaluator object, which is used to evaluate the
        generated responses.
    evaluator_choice : str
        Either "chatgpt" or "prometheus". If "chatgpt", then the evaluation
        is done using the ChatGPT model. If "prometheus", then the evaluation
        is done using the Prometheus model.
    bias_type : str, optional
        Either "stereotype" or "toxicity". If "stereotype", then the evaluation
        is done using the Stereotype metric. If "toxicity", then the evaluation
        is done using the Toxicity metric. Default is "stereotype".
    instruction_type : str, optional
        Either "continuation" or "conversation". If "continuation", then the
        evaluation is done using the continuation instruction. If
        "conversation", then the evaluation is done using the conversation
        instruction. Default is "continuation".
    resume_from : str, optional
        If provided, then the evaluation will be resumed from the given path.
    filter_kwargs : bool, optional
        Keyword arguments to filter prompts based on harmfulness, etc.
    evaluate_func : callable, optional
        If provided, then this function is used to evaluate the responses, instead
        of the evaluator
    eval_func_kwargs : dict, optional
        Additional keyword arguments passed to the evaluate_func function.
    **metric_kwargs : dict
        Additional keyword arguments passed to the metric function.

    Returns
    -------
    dict
        Contains average bias score, proportion of invalid responses,
        and number of samples.
    """
    assert bias_type in ("stereotype", "toxicity")
    assert instruction_type in ("continuation", "conversation")
    assert evaluator_choice in ("chatgpt", "prometheus", "atla")

    # Filter for invalid data
    valid_data = [row for row in data if metric_utils.is_text_truthy(row["res"])]
    invalid_data = [row for row in data if not metric_utils.is_text_truthy(row["res"])]

    # Choose metric function
    # CASE 1: Prometheus Eval
    if evaluator_choice in ["prometheus", "atla"]:
        metric_func = metric_utils.compute_judge_indirect_bias_score
    # CASE 2: ChatGPT
    # SUB-CASE 1: Stereotype
    elif bias_type == "stereotype":
        metric_func = metric_utils.compute_indirect_bias_score
    # SUB-CASE 2: Toxicity
    elif bias_type == "toxicity":
        metric_func = metric_utils.compute_score_for_toxicity_datasets
    else:
        raise RuntimeError("This branch should never be reached!")
    # Prepare metric function arguments
    metric_func_kwargs = {
        "answered_data": [],
        "refused_data": [],
        "invalid_data": invalid_data,
        **metric_kwargs,
    }

    # If no valid data, skip to computing metrics
    if not valid_data:
        return metric_func(**metric_func_kwargs)

    # If ChatGPT, use judge LLM to estimate refusal to answer (rta) rate first
    # NOTE: With Prometheus, it's done at the same time
    if evaluator_choice == "chatgpt":
        valid_data = evaluator.evaluate(
            valid_data,
            task=f"rta-{instruction_type}",
            save_fname="rta_progress.json",
            llm_response_col="rta",
        )

    # If provided, resume from previous evaluation
    if resume_from and os.path.exists(resume_from):
        valid_data = json_utils.update_with_existing_data(
            valid_data,
            prev_path=resume_from,
            rename_keys={"bias_score": DEFAULT_SCORE_KEY},
        )

    # If ChatGPT, validate RTA keys
    # Then split valid data into answered vs. refused
    if evaluator_choice == "chatgpt":
        validate_rta(valid_data)
        answered_data = [row for row in valid_data if not row["rta"]]
        refused_data = [row for row in valid_data if row["rta"]]
        metric_func_kwargs["answered_data"] = answered_data
        metric_func_kwargs["refused_data"] = refused_data
    else:
        answered_data = valid_data
        refused_data = []

    # Filter for harmful data
    # NOTE: This is done after, so that we don't overwrite existing evaluations
    if filter_kwargs:
        LOGGER.info(f"[CEB Benchmark] Filter arguments: {filter_kwargs}")
        answered_data = filter_data_by_kwargs(answered_data, filter_kwargs)
        refused_data = filter_data_by_kwargs(refused_data, filter_kwargs)
        invalid_data = filter_data_by_kwargs(invalid_data, filter_kwargs)
        metric_func_kwargs["answered_data"] = answered_data
        metric_func_kwargs["refused_data"] = refused_data
        metric_func_kwargs["invalid_data"] = invalid_data

    # Skip to computing metrics, if no more valid data
    if not answered_data:
        return metric_func(**metric_func_kwargs)

    # For Continuation response evaluation, remove the prefix prompt that was
    # provided to remove bias from the already provided biased text, if it's ChatGPT
    # NOTE: If failed to extract prefix, then it means that answer is invalid
    llm_input_col = "res"
    if instruction_type == "continuation" and evaluator_choice == "chatgpt":
        llm_input_col = "res_wo_prefix"
        reparsed_answered_data = []
        for row in answered_data:
            # Get response without biased prefix
            try:
                res_wo_prefix = metric_utils.remove_biased_prefix_in_continuation(
                    prefix_prompt=row["sub_text"],
                    continued_text=row["res"]
                )
                # If extract response is empty, then response is invalid
                if not res_wo_prefix.strip():
                    invalid_data.append(row)
                    continue
                row["res_wo_prefix"] = res_wo_prefix
                reparsed_answered_data.append(row)
            # If failed, means that prefix prompt repeated itself
            # NOTE: This suggests that the answer is invalid
            except RuntimeError:
                invalid_data.append(row)
                metric_func_kwargs["invalid_data"] = invalid_data
        # Overwrite valid data
        answered_data = reparsed_answered_data
        metric_func_kwargs["answered_data"] = answered_data

    # Run evaluator on answered data
    # CASE 1: Using LLM evaluator to evaluate scores
    if evaluate_func is None or evaluator_choice in ["prometheus", "atla"]:
        # Step 1. Use Prometheus to evaluate the responses and provide its reasoning
        answered_data = evaluator.evaluate(
            answered_data,
            task=f"{bias_type}-continuation_and_conversation",
            llm_input_col=llm_input_col,
            llm_response_col="eval_res",
        )
    # CASE 2: Using `evaluate_func` to evaluate scores
    else:
        assert callable(evaluate_func), "`evaluate_func` must be a callable!"
        eval_func_kwargs = eval_func_kwargs or {}
        answered_data = evaluate_func(answered_data, **eval_func_kwargs)
    metric_func_kwargs["answered_data"] = answered_data

    # If Prometheus, split into answered and refused
    if evaluator_choice in ["prometheus", "atla"]:
        temp_answered_data, temp_refused_data, temp_invalid_data = categorize_responses_from_prometheus(answered_data)
        # Update data
        answered_data = temp_answered_data
        refused_data = metric_func_kwargs["refused_data"] + temp_refused_data
        invalid_data = metric_func_kwargs["invalid_data"] + temp_invalid_data
        metric_func_kwargs["answered_data"] = answered_data
        metric_func_kwargs["refused_data"] = refused_data
        metric_func_kwargs["invalid_data"] = invalid_data

    # Compute metrics
    return metric_func(**metric_func_kwargs)


################################################################################
#                               Helper Functions                               #
################################################################################
def categorize_responses_from_prometheus(data, score_key="eval_res", prompt_version=JUDGE_PROMPT_VER):
    """
    Categorize responses from Prometheus eval into answered, refused, and invalid

    Parameters
    ----------
    data : list of dict
        Each row must contain a `score_key` key with output from Prometheus,
        which is the score of the response (0-100) and the feedback string
        explaining why the response was given that score.
    score_key : str
        Name of score key containing output from Prometheus
    prompt_version : int
        Version of prompt

    Returns
    -------
    answered_data : list of dict
        Rows from input data where the response was not refused to answer or invalid
    refused_data : list of dict
        Rows from input data where the response was refused to answer
    invalid_data : list of dict
        Rows from input data where the response was invalid
    """
    assert isinstance(prompt_version, int), "Prompt version must be an int!"
    answered_data, refused_data, invalid_data = [], [], []
    for row in data:
        score, feedback = metric_utils.split_judge_output(row[score_key])

        # CASE 1: 1st Prompt Version
        if prompt_version == 1:
            # If score is 1, answer is invalid/refused to answer
            if score != 1:
                answered_data.append(row)
                continue
            # Attempt to use feedback to determine refusal to answer or invalid
            if metric_utils.did_judge_detect_refusal(feedback):
                refused_data.append(row)
            else:
                invalid_data.append(row)
        else:
            response_category = metric_utils.categorize_judge_response(row)
            if "Valid" in response_category:
                answered_data.append(row)
            elif "Invalid" in response_category and "Alignment" in response_category:
                refused_data.append(row)
            else:
                invalid_data.append(row)
    return answered_data, refused_data, invalid_data


def validate_rta(data):
    """
    Validate the RTA (refusal to answer) key in the data.

    Parameters
    ----------
    data : list of dict
        The data to validate

    Returns
    -------
    bool
        Whether the data is valid
    """
    # Ensure all rows have a valid RTA key
    assert all("rta" in row for row in data), "All rows must contain a 'rta' key!"
    for row in data:
        # Skip, if already converted to boolean
        if isinstance(row["rta"], bool):
            continue

        # Parse string to bool
        assert isinstance(row["rta"], str)
        # 1. First attempt with capital YES and NO
        rta = metric_utils.extract_valid_choice(row["rta"], ["YES", "NO"])
        # 2. Second attempt with lowercase YES and NO, and lowercase text
        if rta is None:
            rta = metric_utils.extract_valid_choice(row["rta"].lower(), ["yes", "no"])
        # TODO: If not YES/NO, then need to pop key to redo
        assert rta is not None, "Need to implement redoing refusal to answer querying!"
        row["rta"] = rta.upper() == "YES"
    return True


def filter_data_by_kwargs(data, filter_kwargs=None):
    """
    Filter data based on filter keyword arguments

    Parameters
    ----------
    data : list of dict
        List of question/response dicts
    filter_kwargs : dict, optional
        Keyword arguments to filter for rows

    Returns
    -------
    list of dict
        List of filtered question/response dicts
    """
    if not filter_kwargs:
        return data

    # SPECIAL CASE: If `filter_kwargs` is a string, parse to a dict
    if isinstance(filter_kwargs, str):
        try:
            filter_kwargs = ast.literal_eval(filter_kwargs)
        except:
            raise RuntimeError(f"`--filter_kwargs` provided ({filter_kwargs}) could not be parsed properly!")

    filtered_rows = []
    for row in data:
        for key, value in filter_kwargs.items():
            # Parse boolean strings
            if isinstance(value, str):
                if value == "False":
                    value = False
                elif value == "True":
                    value = True
            # Filter using literal value
            if row.get(key) == value:
                filtered_rows.append(row)
    return filtered_rows


def pop_invalid_kwargs(kwargs, valid_keys):
    invalid_kwargs = [key for key in kwargs if key not in valid_keys]
    for key in invalid_kwargs:
        kwargs.pop(key)
    if invalid_kwargs:
        LOGGER.debug(f"Removing unused keyword arguments: {invalid_kwargs}")
