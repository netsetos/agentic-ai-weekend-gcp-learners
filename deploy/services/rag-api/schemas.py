from typing import Literal, Optional
from pydantic import BaseModel, Field

# THE contract lives in shared/ - one definition for 3.2, 4.2, this service and every agent.
# Gap G1: until 2026-09-05 this file carried its own Citation/RAGAnswer, the third of three.
from shared.documind_schemas import Citation, DraftCitation, ModelDraft, RAGAnswer, resolve  # noqa: F401

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
    filters: Optional[dict] = None  # e.g. {"doc_type": "policy"}
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
    latency_ms: int

class StreamEvent(BaseModel):
    # Server-Sent Events payload
    event: Literal["token", "citation", "done", "error"]
    data: dict
