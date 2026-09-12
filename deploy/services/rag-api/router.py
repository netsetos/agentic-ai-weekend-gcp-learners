"""Model router for the DocuMind rag-api (lesson 3.3). Wired into /v1/query by lesson 12.6; standalone until then."""
from enum import Enum

from google.genai import types


class Complexity(str, Enum):
    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"


# Same routing table as lesson 3.3. No temperature key: Gemini 3.6 Flash ignores
# temperature/top_p/top_k and Google says to leave the default on every Gemini 3 model.
# COMPLEX cap is 16384 because thinking tokens share max_output_tokens.
ROUTING_TABLE = {
    Complexity.SIMPLE:  {"model": "gemini-3.1-flash-lite", "thinking_budget": 0,    "max_output_tokens": 512},
    Complexity.MEDIUM:  {"model": "gemini-3.6-flash",      "thinking_budget": 1024, "max_output_tokens": 2048},
    Complexity.COMPLEX: {"model": "gemini-3.1-pro-preview", "thinking_budget": 8192, "max_output_tokens": 16384},
}

CLASSIFIER_MODEL = "gemini-3.1-flash-lite"

# The client is passed in because the service already owns a global-endpoint
# genai.Client in generator.py (Gemini 3.x generation is served from global only).


def classify(query: str, client) -> Complexity:
    """Classify a query into SIMPLE / MEDIUM / COMPLEX with the lesson 3.3 Step 4 criteria."""
    r = client.models.generate_content(
        model=CLASSIFIER_MODEL,
        contents=f"""Classify this query's complexity level.

SIMPLE: Factual lookups, greetings, definitions, yes/no, classification, extraction
MEDIUM: Explanations, comparisons, summaries, standard code, multi-paragraph writing
COMPLEX: Multi-step math, architecture design, debugging, proofs, strategic reasoning

Query: {query}""",
        config=types.GenerateContentConfig(
            response_mime_type="text/x.enum",
            response_schema={"type": "STRING", "enum": [c.value for c in Complexity]},
            thinking_config=types.ThinkingConfig(thinking_budget=0),  # lowest level, not off
        ),
    )
    return Complexity(r.text.strip())


def route(query: str, client, system_instruction: str = "", schema=None) -> dict:
    """Classify, pick the tier, generate. Mirrors lesson 3.3 Step 10 route()."""
    complexity = classify(query, client)
    cfg = ROUTING_TABLE[complexity]
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=cfg["max_output_tokens"],
        thinking_config=types.ThinkingConfig(thinking_budget=cfg["thinking_budget"]),
    )
    if schema:
        config.response_mime_type = "application/json"
        config.response_schema = schema
    r = client.models.generate_content(model=cfg["model"], contents=query, config=config)
    return {
        "text": r.text,
        "complexity": complexity.value,
        "model": cfg["model"],
        "think_tokens": r.usage_metadata.thoughts_token_count or 0,
    }
