"""Model Armor, on both sides of the model."""
import os

from google.cloud import modelarmor_v1

LOCATION = os.environ.get("ARMOR_LOCATION", "asia-south1")
TEMPLATE = os.environ.get("ARMOR_TEMPLATE", "documind-guard")
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]

_client = modelarmor_v1.ModelArmorClient(
    client_options={"api_endpoint": f"modelarmor.{LOCATION}.rep.googleapis.com"})
_template = f"projects/{PROJECT}/locations/{LOCATION}/templates/{TEMPLATE}"


def _blocked(result) -> bool:
    """MATCH_FOUND on any filter means the content is not safe to use."""
    return any(
        r.match_state == modelarmor_v1.FilterMatchState.MATCH_FOUND
        for r in result.sanitization_result.filter_results.values())


def check_prompt(text: str) -> tuple[bool, str]:
    """Screen the USER's text before it reaches retrieval.

    Before retrieval, not after: a prompt injection that reaches the retriever
    has already chosen which documents the model will read, and no amount of
    output filtering un-chooses them.
    """
    r = _client.sanitize_user_prompt(
        request=modelarmor_v1.SanitizeUserPromptRequest(
            name=_template,
            user_prompt_data=modelarmor_v1.DataItem(text=text)))
    return (not _blocked(r)), "prompt_blocked"


def check_response(text: str) -> tuple[bool, str]:
    """Screen the MODEL's answer before the user sees it.

    This runs on the BUFFERED final answer, never on the stream. A guard that
    inspects tokens as they fly past has already shown the user the first half
    of whatever it was going to block.
    """
    r = _client.sanitize_model_response(
        request=modelarmor_v1.SanitizeModelResponseRequest(
            name=_template,
            model_response_data=modelarmor_v1.DataItem(text=text)))
    return (not _blocked(r)), "response_blocked"
