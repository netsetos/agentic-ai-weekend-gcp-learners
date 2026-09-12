"""DocuMind's A2A peer - an agent OUTSIDE the kit, reaching the lane only through documind-mcp.

Lesson 8.4. Module 7 ended on one sentence: the MCP server is for agents outside the kit, and
the brains inside the kit call retrieve() directly. This service is that sentence with an
address. It is the agent another team - another company - would build: it knows DocuMind by
ONE URL (MCP_URL), speaks to it as its own account (documind-agent-sa, on one roster), and is
itself reachable by other agents over A2A: an agent card at /.well-known/agent-card.json and
JSON-RPC at /. Three protocols meet in one request:

    A2A client --ID token for THIS service--> here --McpToolset, ID token for documind-mcp--> documind-mcp
                                                                 (verifies agent-sa, checks the roster)
                                                                     --retrieve() as documind-mcp-sa--> documind-api

What is deliberately NOT here: `from shared import ...`. No retrieve(), no roster, no verifier.
The image copies only this directory (Dockerfile) and the gate in tools/check_auth_wiring.py
fails the build if a kit import appears. That absence is the lesson: a peer that imported the
kit would be a fifth brain, not a peer.

Identity, precisely. Cloud Run IAM decides who may call THIS service (roles/run.invoker on
documind-agent: ui-sa and chat-sa). The A2A protocol carries no identity of its own, so the
peer speaks to the lane as ITSELF - never as the caller - and the roster sees documind-agent-sa.
That is why the account sits on exactly one roster (acme, deploy/Makefile `roster`): a peer
scoped to one tenant needs no tenant argument, and a request naming another tenant is refused
by the server, not by this code.

The credential refreshes: `header_provider` is called by ADK on EVERY tool call
(google/adk/tools/mcp_tool/mcp_tool.py, _run_async_impl), so the token is minted fresh each
time - a Cloud Run ID token lasts an hour, and a token captured once is 7.3's bug with an
hour-long fuse. On Cloud Run the metadata server mints it as the service account; in a
notebook, DOCUMIND_IMPERSONATE_SA mints as a roster member (the same hook 7.1 taught).
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("documind.agent")

MCP_URL = os.environ.get("MCP_URL", "").rstrip("/")     # https://documind-mcp-NUMBER.us-central1.run.app
SELF_URL = os.environ.get("SELF_URL", "").rstrip("/")   # this service's URL - the address on its agent card
MODEL = os.environ.get("AGENT_MODEL", "gemini-3.6-flash")
# id-token (Cloud Run, or a notebook with DOCUMIND_IMPERSONATE_SA) | none (the 7.1 local server, no IAM)
MCP_AUTH = os.environ.get("MCP_AUTH", "id-token")

INSTRUCTION = (
    "You are DocuMind, an agent that answers questions about a company's documents. "
    "You hold no documents yourself: for any question about documents call retrieve, answer only "
    "from what it returns, and cite the sources it names. Use list_documents to see what the corpus "
    "holds, corpus_stats for counts, and calculate_processing_cost to price a set of pages. "
    "Do not pass a tenant argument unless the person names a tenant: your own account decides which "
    "corpus is yours, and the server refuses a tenant you are not on. "
    "If a tool refuses, or says the corpus cannot answer, say so plainly and cite nothing."
)


def _mcp_headers(readonly_context=None) -> dict:
    """The credential for documind-mcp, minted per call.

    ADK calls this on every tool call, so nothing here is cached. The audience is the server's
    ROOT url: Cloud Run checks the token was minted for it, and the server checks it again as
    its SELF_URL (7.1). With DOCUMIND_IMPERSONATE_SA set - a notebook, a laptop - the token is
    minted AS that account, which is how a person runs this peer before it has an account.
    """
    if MCP_AUTH == "none":
        return {}
    if not MCP_URL:
        raise RuntimeError("MCP_URL is not configured - the peer has no lane to reach")
    import google.auth
    import google.auth.transport.requests
    import google.oauth2.id_token

    request = google.auth.transport.requests.Request()
    impersonate = os.environ.get("DOCUMIND_IMPERSONATE_SA")
    if impersonate:
        from google.auth import impersonated_credentials
        source, _ = google.auth.default()
        target = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=impersonate,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"])
        idc = impersonated_credentials.IDTokenCredentials(target, target_audience=MCP_URL, include_email=True)
        idc.refresh(request)
        return {"Authorization": f"Bearer {idc.token}"}
    return {"Authorization": f"Bearer {google.oauth2.id_token.fetch_id_token(request, MCP_URL)}"}


def build_agent() -> LlmAgent:
    """The peer. Its only tools are the lane's, discovered from the server at first use."""
    lane = McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{MCP_URL}/mcp",
            # ADK's default HTTP timeout is 5 s. A retrieve() through the server is rag-api plus a
            # Gemini answer - seconds when warm, up to 90 s when the API is cold (7.2) - while a
            # roster refusal is instant. The first live smoke passed the zeta task and failed the
            # acme one on exactly this line. 120 s covers the server's own RAG_TIMEOUT_S=90.
            timeout=120,
            sse_read_timeout=300,
        ),
        header_provider=_mcp_headers,
    )
    return LlmAgent(
        name="documind_peer",
        model=MODEL,
        description="Answers questions about DocuMind's documents with citations, "
                    "through the DocuMind MCP server. Prices document processing.",
        instruction=INSTRUCTION,
        tools=[lane],
    )


def _card_address() -> tuple[str, int, str]:
    """host, port, protocol for the agent card - the deployed URL, not localhost.

    to_a2a builds the card's url as protocol://host:port/ and A2A clients post to exactly that,
    so a card that says localhost is a peer nobody can reach. SELF_URL is the deterministic
    run.app address (deploy/commands/lesson-8.4.sh); unset, the peer is on a laptop.
    """
    if SELF_URL:
        u = urlparse(SELF_URL)
        return u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80), u.scheme or "https"
    return "localhost", int(os.environ.get("PORT", "8080")), "http"


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "mcp_url": MCP_URL or None, "self_url": SELF_URL or None,
                         "model": MODEL, "kit_import": False})


agent = build_agent()
_host, _port, _protocol = _card_address()
app = to_a2a(agent, host=_host, port=_port, protocol=_protocol)
app.add_route("/health", health, methods=["GET"])
log.info('{"event":"agent_up","card":"%s://%s:%s/.well-known/agent-card.json","mcp_url":"%s"}',
         _protocol, _host, _port, MCP_URL)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
