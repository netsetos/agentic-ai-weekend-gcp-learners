"""DocuMind chat service - the tool layer.

One definition of each tool, imported by the agent and by nothing else. Lessons 6.1 to 6.4 all
teach against these signatures; keeping them here means a lesson and the deployed service cannot
disagree about what `calculate_processing_cost` means, which is exactly the drift Module 6 opened
with (the same tool carried three different shapes across three lessons).

Two things differ from the notebook versions, and both are about identity rather than logic:

  - `retrieve` ADAPTS shared/documind_tools.retrieve - binding the tenant, renaming the
    filter and reshaping the response - instead of returning a mock dict. It does not talk to
    rag-api itself; there is exactly one function in this repo that does (lesson 8.7).
  - it and `get_usage_stats` read the tenant (and the person's IAP assertion) from the
    ToolRuntime the framework injects, never from an argument the model fills.

HOW THE TENANT REACHES A TOOL, precisely, because the first version got it wrong. `tenant_id`
was declared `Annotated[str, InjectedToolArg]`, which HIDES an argument from the schema the
model reads - and does nothing else. langgraph's ToolNode fills `InjectedState`, `InjectedStore`
and `ToolRuntime`; a bare `InjectedToolArg` is never filled, so the tool ran with tenant_id=""
and rag-api answered 403 for a tenant that does not exist. Hidden from the model AND delivered
is `runtime: ToolRuntime`, whose `.context` is the dict agent.py passes to
`agent.invoke(..., context=...)`. Proven offline on 2026-09-05 (gap G4): the runtime parameter
is absent from `tool_call_schema`, and the context arrives.

`calculate_processing_cost` is byte-for-byte the lesson's version. Everything else about the loop
was already proven in the notebook, so those are deliberately the only changes.

Verified 2026-09-04 against langchain 1.4.0 / langchain-google-genai 4.4.0; the ToolRuntime
wiring against langgraph 1.2.11 / langchain-core 1.5.6 on 2026-09-05.
"""
from __future__ import annotations

import logging
import time

from langchain_core.tools import tool

try:
    from langchain.tools import ToolRuntime          # langchain 1.x re-exports langgraph's
except ImportError:                                   # a bare langgraph install
    from langgraph.prebuilt.tool_node import ToolRuntime

# The shared tool layer. The image must be built with `deploy/` as its context so that
# shared/ lands beside this service - see the Dockerfile. Lesson 8.7 is the argument for why
# this import exists at all: one retrieval implementation, adapted per brain, never re-written.
from shared import documind_tools

logger = logging.getLogger("documind.chat.tools")

USD_INR = 85  # course-wide conversion rate
RATES = {"standard": 0.05, "priority": 0.12, "bulk": 0.03}

# Tools that must never be reachable from a model turn. GuardMiddleware in agent.py imports this
# set and refuses these names before dispatch; keeping it beside the tools makes an omission
# visible in review rather than at 2am.
BLOCKED = {"delete_document", "send_email", "modify_access"}
TIMEOUTS = {"retrieve": 30, "calculate_processing_cost": 10, "get_usage_stats": 60}


def _ctx(runtime: ToolRuntime, key: str, default: str = "") -> str:
    """One value out of the runtime context agent.py set - a dict, or an object with attributes."""
    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, dict):
        return ctx.get(key, default)
    return getattr(ctx, key, default) if ctx is not None else default


@tool
def retrieve(query: str, doc_type: str = "all", top_k: int = 5,
             runtime: ToolRuntime = None) -> dict:
    """Retrieve grounded passages from DocuMind's corpus.

    Args:
        query: The question, in natural language
        doc_type: Filter by type (policy, contract, invoice, form, research_paper, all)
        top_k: How many passages to return
    """
    # runtime is INJECTED by the framework and absent from the schema the model reads (see the
    # module docstring). Its context carries the tenant, looked up from the roster by agent.py,
    # and the IAP assertion of the person this turn is for. Declare either as an ordinary
    # parameter and the model chooses the tenant - a cross-tenant read that no docstring can
    # prevent. Both are absent from the Args block on purpose: that block is model-facing.
    tenant_id = _ctx(runtime, "tenant_id")
    assertion = _ctx(runtime, "assertion")
    brain = _ctx(runtime, "brain")          # which harness is asking - rag-api's usage row records it (8.7)
    # ADAPTER, NOT IMPLEMENTATION (lesson 8.7). This function binds the tenant, renames the
    # filter and reshapes the response for the chat API's contract. What it does NOT do is talk
    # to rag-api itself - that is documind_tools.retrieve's job, and there is exactly one of it
    # (and it is also where DOCUMIND_PROFILE=local turns into a Chroma read, gap G3).
    started = time.monotonic()
    try:
        answer = documind_tools.retrieve(
            query, tenant_id=tenant_id, top_k=top_k,
            doc_type=None if doc_type == "all" else doc_type,
            assertion=assertion or None, brain=brain or None)
    finally:
        logger.info("retrieve took %.2fs", time.monotonic() - started)

    if "error" in answer:
        # Returned as data, not raised. The model reads the failure and says so, which is the
        # rule lesson 6.2 set: an exception kills the turn, a payload lets the agent explain.
        logger.warning("rag-api query failed: %s", answer["error"])
        return {"error": "document search is unavailable",
                "citations": [], "answerable": False, "confidence": "low"}

    citations = answer.get("citations", [])
    return {
        "citations": [
            # Widened for lesson 9.6. A projection is a SILENT filter: name five
            # keys here and a media citation arrives as plain text with no
            # thumbnail and no timestamp, with nothing raised and nothing logged.
            {k: c[k] for k in ("chunk_id", "source_uri", "page", "quote", "score",
                               "kind", "media_url", "start", "end") if k in c}
            for c in citations
        ],
        "answerable": answer.get("answerable", False),
        "confidence": answer.get("confidence", "low"),
    }


@tool
def calculate_processing_cost(total_pages: int, num_documents: int = 1,
                              processing_type: str = "standard") -> dict:
    """Estimate document processing cost in USD and INR.

    Args:
        total_pages: Total page count across all documents
        num_documents: How many documents those pages are spread across
        processing_type: Service tier - standard, priority, or bulk
    """
    rate = RATES.get(processing_type, RATES["standard"])
    cost = total_pages * rate
    return {
        "num_documents": num_documents,
        "total_pages": total_pages,
        "processing_type": processing_type,
        "rate_per_page": rate,
        "cost_usd": round(cost, 2),
        "cost_inr": round(cost * USD_INR, 2),
    }


@tool
def get_usage_stats(metric: str, days: int = 7, runtime: ToolRuntime = None) -> dict:
    """Get DocuMind RAG pipeline usage statistics.

    Args:
        metric: Which metric to retrieve (queries, costs, latency, users)
        days: Number of days to look back
    """
    # Injected, for the same reason as retrieve.
    tenant_id = _ctx(runtime, "tenant_id")
    # Production reads the BigQuery query_logs table from Module 5. Stubbed here so the tool
    # layer stays importable without a warehouse credential; the shape is the contract.
    return {"metric": metric, "tenant_id": tenant_id, "period": f"last {days} days",
            "value": None, "source": "bigquery:query_logs"}


TOOLS = [retrieve, calculate_processing_cost, get_usage_stats]
