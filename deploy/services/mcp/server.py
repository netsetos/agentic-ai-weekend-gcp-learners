"""DocuMind MCP server - the lane's tool surface for agents you did not build (lesson 7.1).

The UI is how a person reaches the lane; this is how an agent reaches it: Claude Desktop,
Cursor, another team's ADK app, lesson 7.3's agent. It is built the way the UI was built,
and for the same reasons:

  - a Cloud Run service with its own identity (documind-mcp-sa, terraform/sa.tf) that sits
    on the tenant rosters beside the UI's account;
  - the caller is verified by the same code the API uses (shared/iap.py, the bearer leg: an
    ID token minted for THIS service's URL, carrying a verified email);
  - the tenant comes from that identity and the roster (shared/tenancy.py) - never from a
    tool argument the model filled in, and never from a header anyone can set. A named
    tenant is accepted only after the roster confirms the caller is on it;
  - retrieval is the ONE implementation (shared/documind_tools.retrieve), which calls the
    API as this service's account. This file owns no index and must not grow one: an MCP
    server that starts caching documents is a second source of truth, and the day the two
    disagree is the day you stop trusting both.

Four tools, all of them the lane's own operations:

    retrieve                   grounded passages and the API's answer, in the citations contract
    list_documents             what is in the caller's corpus - the ingest worker's claims (12.5)
    corpus_stats               chunks and documents by type and by kind
    calculate_processing_cost  the six-key estimate; the tool-chaining demo (7.3)

Two lanes, one file (6.4's switch). DOCUMIND_PROFILE=gcp is the deployment above. With
DOCUMIND_PROFILE=local there is no token and no roster: the caller is LOCAL_USER, the tenant
LOCAL_TENANT, and retrieve() reads the Chroma directory shared/profile.py opens - the same
stand-in the chat service uses, and the Rs 0 lane of 13.2.

Transport: streamable HTTP at /mcp, STATELESS - every request carries its own identity, so an
instance keeps no session memory and Cloud Run can scale it freely. GET /health is for the
platform. Run it locally with

    PYTHONPATH=deploy SELF_URL=http://localhost:8000 RAG_API_URL=https://documind-api-NUMBER.us-central1.run.app \\
    DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com \\
    uvicorn services.mcp.server:app --port 8000

and deployed through commands/lesson-7.2.sh (`make build deploy-services`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

# The image copies deploy/shared beside this file (Dockerfile). A checkout runs it with
# PYTHONPATH=deploy, or as a script, in which case deploy/ is two directories up.
try:
    from shared import documind_tools, iap, tenancy
except ImportError:  # pragma: no cover - the script layout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from shared import documind_tools, iap, tenancy

log = logging.getLogger("documind.mcp")
logging.basicConfig(level=logging.INFO, format="%(message)s")

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
SELF_URL = os.environ.get("SELF_URL", "")            # the audience every bearer token must carry
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")    # set -> shared/audit_log rows; unset -> the log line only
DOC_TYPES = ("policy", "contract", "invoice", "report", "statute", "guidance", "form", "research_paper")

mcp = FastMCP(
    "DocuMind",
    instructions=(
        "DocuMind answers questions about a tenant's documents with citations. Use `retrieve` "
        "for any question about the documents; use `list_documents` to see what the corpus "
        "holds; `corpus_stats` for counts; `calculate_processing_cost` to price a set of pages. "
        "Never state a figure that a citation does not carry."
    ),
)


# ------------------------------------------------------------------ who is calling, for whom
def _caller() -> dict:
    """The identity behind this request: the bearer token's verified email, or the local stand-in."""
    if documind_tools.PROFILE == "local":
        return {"email": os.environ.get("LOCAL_USER", "dev@documind.local"), "via": "local"}
    if not SELF_URL:
        raise ToolError("SELF_URL is not configured on this server, so it cannot verify who is calling")
    try:
        headers = get_http_request().headers
    except Exception as e:  # noqa: BLE001 - an in-process client, no HTTP request to read
        raise ToolError(f"no HTTP request to read an identity from ({type(e).__name__})") from None
    try:
        return iap.identity(headers, bearer_audience=SELF_URL)
    except iap.IapError as e:
        # 401 in spirit: the token is missing, for another audience, or carries no email.
        raise ToolError(f"not authenticated: {e}") from None


def _tenant_for(caller: dict, named: str | None) -> str:
    """The tenant this call is for - from the roster, never from the argument alone.

    A caller on one roster needs no argument. A caller on several (an operator, or a
    notebook minting as documind-ui-sa) names one, and the roster must agree; naming a
    tenant you are not on is the 403 the API would give, raised here before the API is called.
    """
    if documind_tools.PROFILE == "local":
        return named or os.environ.get("LOCAL_TENANT", "acme")
    email = caller["email"]
    if named:
        if not tenancy.is_member(email, named):
            raise ToolError(f"{email} is not on tenant {named!r}'s roster")
        return named
    tenant = tenancy.tenant_for(email)
    if not tenant:
        raise ToolError(f"{email} is on no tenant's roster - an operator adds members with `make roster`")
    return tenant


def _audit(caller: dict, tenant: str, tool: str, meta: dict) -> None:
    """One line per call, always; one bucket row too when the audit bucket is configured."""
    log.info(json.dumps({"event": "mcp_call", "tool": tool, "tenant": tenant,
                         "caller": caller.get("email"), "via": caller.get("via"), **meta}))
    if AUDIT_BUCKET:
        try:
            from shared import audit_log
            audit_log.emit("query.submit",
                           actor={"email": caller.get("email"), "via": caller.get("via"), "tenant_id": tenant},
                           target={"surface": "mcp", "tool": tool, "tenant_id": tenant}, meta=meta)
        except Exception as e:  # noqa: BLE001 - the answer must not depend on the audit bucket
            log.warning('{"event":"mcp_audit_failed","error":"%s"}', type(e).__name__)


def _db():
    from google.cloud import firestore
    return firestore.Client(project=PROJECT or None)


# ------------------------------------------------------------------ the four tools
@mcp.tool(name="retrieve")
def retrieve(query: str, doc_type: str = "all", top_k: int = 5, tenant: str | None = None) -> dict:
    """Retrieve grounded passages from DocuMind's corpus, with the lane's own cited answer.

    Args:
        query: The question, in natural language.
        doc_type: policy, contract, invoice, report, statute, guidance, form, research_paper, or all.
        top_k: How many passages to return (1-20).
        tenant: Only if you belong to several tenants - which one. Checked against the roster.
    """
    if not query.strip():
        # A caller bug, not a runtime condition: the caller is the one who can fix it.
        raise ToolError("query cannot be empty")
    if doc_type not in ("all", "") and doc_type not in DOC_TYPES:
        raise ToolError(f"doc_type must be one of {DOC_TYPES} or all, not {doc_type!r}")
    caller = _caller()
    tenant_id = _tenant_for(caller, tenant)
    out = documind_tools.retrieve(query, tenant_id, top_k=max(1, min(int(top_k), 20)),
                                  doc_type=None if doc_type in ("all", "") else doc_type, brain="mcp")
    _audit(caller, tenant_id, "retrieve",
           {"query_sha": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            "answerable": out.get("answerable"), "citations": len(out.get("citations") or []),
            "error": out.get("error")})
    return out                       # the citations contract, or {"error": ...} as DATA the model can read


@mcp.tool
def list_documents(status: str = "indexed", tenant: str | None = None) -> dict:
    """What is in the caller's corpus: one row per uploaded document, from the ingest worker's claims.

    Args:
        status: indexed, processing, failed, or all.
        tenant: Only if you belong to several tenants - which one.
    """
    if status not in ("indexed", "processing", "failed", "all"):
        raise ToolError("status must be indexed, processing, failed or all")
    caller = _caller()
    tenant_id = _tenant_for(caller, tenant)
    if documind_tools.PROFILE == "local":
        docs = _local_documents(tenant_id)
    else:
        docs = []
        prefix = f"{tenant_id}_"                                   # documents/{tenant}_{sha256} (12.5)
        for snap in _db().collection("documents").stream():
            if not snap.id.startswith(prefix):
                continue
            d = snap.to_dict() or {}
            if status != "all" and d.get("status") != status:
                continue
            when = d.get("indexed_at") or d.get("failed_at") or d.get("claimed_at")
            docs.append({"file": (d.get("gcs_uri") or "").rsplit("/", 1)[-1], "status": d.get("status"),
                         "chunks": d.get("chunks"), "at": when.isoformat() if hasattr(when, "isoformat") else when,
                         "error": d.get("error")})
        docs.sort(key=lambda x: x["file"])
    _audit(caller, tenant_id, "list_documents", {"status": status, "documents": len(docs)})
    return {"tenant": tenant_id, "status": status, "documents": docs}


@mcp.tool
def corpus_stats(tenant: str | None = None) -> dict:
    """Chunks and documents in the caller's corpus, by document type and by kind (text, figure, table, segment).

    Args:
        tenant: Only if you belong to several tenants - which one.
    """
    caller = _caller()
    tenant_id = _tenant_for(caller, tenant)
    by_type: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    sources: set[str] = set()
    for meta in _chunk_metas(tenant_id):
        by_type[meta.get("doc_type") or "unknown"] = by_type.get(meta.get("doc_type") or "unknown", 0) + 1
        by_kind[meta.get("kind") or "text"] = by_kind.get(meta.get("kind") or "text", 0) + 1
        if meta.get("source_uri"):
            sources.add(meta["source_uri"])
    out = {"tenant": tenant_id, "chunks": sum(by_type.values()), "documents": len(sources),
           "by_doc_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])), "by_kind": by_kind}
    _audit(caller, tenant_id, "corpus_stats", {"chunks": out["chunks"], "documents": out["documents"]})
    return out


@mcp.tool
def calculate_processing_cost(total_pages: int, num_documents: int = 1, processing_type: str = "standard") -> dict:
    """Estimate document processing cost in USD and INR.

    Args:
        total_pages: Total page count across all documents.
        num_documents: How many documents those pages are spread across.
        processing_type: Service tier - standard, priority, or bulk.
    """
    try:
        return documind_tools.calculate_processing_cost(int(total_pages), int(num_documents), processing_type)
    except ValueError as e:
        raise ToolError(str(e)) from None


# ------------------------------------------------------------------ the two stores, read only
def _chunk_metas(tenant_id: str) -> list:
    if documind_tools.PROFILE == "local":
        from shared.profile import build_store
        got = build_store().get(where={"tenant_id": tenant_id}, include=["metadatas"])
        return list(got.get("metadatas") or [])
    from google.cloud.firestore_v1.base_query import FieldFilter
    q = (_db().collection("chunks").where(filter=FieldFilter("tenant_id", "==", tenant_id))
         .select(["doc_type", "kind", "source_uri"]))               # never the 768 floats
    return [snap.to_dict() or {} for snap in q.stream()]


def _local_documents(tenant_id: str) -> list:
    """The local lane has no claims table: a document is a distinct source_uri in the store."""
    files: dict[str, int] = {}
    for meta in _chunk_metas(tenant_id):
        name = (meta.get("source_uri") or "").rsplit("/", 1)[-1]
        if name:
            files[name] = files.get(name, 0) + 1
    return [{"file": f, "status": "indexed", "chunks": n, "at": None, "error": None} for f, n in sorted(files.items())]


# ------------------------------------------------------------------ the app
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "profile": documind_tools.PROFILE, "self_url": SELF_URL or None})


app = mcp.http_app(path="/mcp", stateless_http=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
