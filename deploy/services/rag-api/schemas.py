from typing import Literal, Optional
from pydantic import BaseModel, Field

# THE contract lives in shared/ - one definition for 3.2, 4.2, this service and every agent.
# Gap G1: until 2026-09-05 this file carried its own Citation/RAGAnswer, the third of three.
from shared.documind_schemas import Citation, DraftCitation, ModelDraft, RAGAnswer, resolve  # noqa: F401

# The filter keys a caller may send (12 September 2026): each one is a restrict namespace the indexer writes on
# the datapoint AND a field on the Firestore row, so the same predicate holds on the dense path, the hybrid path
# and the Firestore fallback alike. tenant_id is the roster's and `current` is the ledger's - never the body's: a
# caller naming either is a header in disguise. main.py refuses any other key with a 400 (check_filters).
FILTER_KEYS = ("doc_type", "kind")

class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    tenant_id: str = Field(min_length=1)
    # Optional, and unread: the caller's identity is the verified assertion or bearer token
    # (auth.py), never a body field - a user named in the body is a header in disguise.
    # It stays accepted because Module 4's notebooks still send one; required, it refused the
    # UI, which rightly sends none, with a 422 on the first live sign-in.
    user_id: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=20)
    stream: bool = True
    filters: Optional[dict] = None  # e.g. {"doc_type": "policy"}: keys from FILTER_KEYS, string values
    # Which harness is asking (8.7, gap G6): the chat service's brains and the UI label
    # themselves so usage_row - and therefore tenant_daily - can compare them. A label only;
    # nothing in retrieval or generation reads it. "mcp" is the agent surface (7.1-7.2): its
    # first live call was a 422, because a new surface has to be added to this list - the
    # list is closed on purpose, so an unknown label is a typo and not a new row in the warehouse.
    brain: Optional[Literal["langchain", "langgraph", "adk", "direct", "ui", "mcp"]] = None

class RAGResponse(RAGAnswer):
    """The contract plus the transport envelope. RAGAnswer is what every module passes along;
    model/tokens/latency are what THIS service knows about the call, and they do not belong
    in a schema 3.2 asks Gemini to fill."""
    model: str
    # Module 11: which backend answered (vertex | gateway) and, from the gateway, what it priced the answer at.
    backend: str = "vertex"
    cost_usd: Optional[float] = None
    tokens_in: int
    tokens_out: int
    # 10.2's context cache, on the answer (12 September 2026): the prompt tokens the model served from it, INSIDE
    # tokens_in and priced at the cache rate (cost.py). Every paid attempt is summed here - the truncation retry's
    # first attempt included - so the row's cost_usd is what was billed.
    cached_tokens: int = 0
    latency_ms: int
    # Where the time went (main.py stage()): retrieve_ms, rerank_ms, generate_ms and the pool the reranker saw.
    # The usage row carries the same four flat, for tenant_daily; here they ride together, for the caller.
    stages: dict[str, int | str] = Field(default_factory=dict)
    # 12.6's answer cache: "semantic" when this answer was served from it (backend=cache, cost 0), else "none".
    cache_hit: Literal["none", "semantic"] = "none"

class StreamEvent(BaseModel):
    # Server-Sent Events payload
    event: Literal["token", "citation", "done", "error"]
    data: dict
