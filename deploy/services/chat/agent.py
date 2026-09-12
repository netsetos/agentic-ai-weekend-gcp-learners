"""DocuMind chat service - the surface, the identity, the memory, and the brain switch.

This is the production half of lesson 6.4, grown by 8.5 (a checkpointer), 8.7 (three brains
over one tool) and 12.8 (one identity). The brains live in `brains.py`; this file decides who
is asking, which tenant they are, which conversation this is, and which brain answers - then
logs one row saying so.

Six things here are load-bearing and easy to get wrong:

0. THE TENANT IS NOT A REQUEST FIELD. It used to be, and that made every document in every
   tenant readable with one curl. It is now looked up from the Firestore roster using the
   email on the verified IAP assertion. The retriever's tenant filter was always correct;
   it was filtering on a value the caller chose.

1. `wrap_tool_call` must return a `ToolMessage` or a `Command`, never a bare dict (brains.py).
2. `tenant_id` reaches the tools through the `ToolRuntime` the framework injects - its
   `.context` is the dict passed to `invoke(..., context=...)` - so it never appears in the
   schema the model reads. If it were an ordinary parameter the model would choose the tenant,
   which is a cross-tenant read wearing the costume of a tool argument. It was first written
   as `Annotated[str, InjectedToolArg]`, which hides an argument from the model and does
   nothing else: ToolNode never fills it, the tool ran with tenant_id="" and rag-api answered
   403 (proven offline 2026-09-05, gap G4). The person's IAP assertion travels the same way:
   read off this request, put in the context, forwarded to rag-api by the one `retrieve()`
   beside the service's own ID token - never sent as a header anyone could set.
3. THE PROFILE SWITCH IS 6.4's, UNCHANGED (gap G3). `shared/profile.py` decides the model
   (Gemini on Vertex AI, or Ollama gemma3:4b) and `documind_tools.retrieve()` decides the store
   (rag-api, or a Chroma directory). With DOCUMIND_PROFILE=local there is no IAP in front of
   this service either, so identity comes from LOCAL_USER / LOCAL_TENANT - a dev-only bypass
   that is explicit, named, and impossible to reach with the profile set to gcp.
4. A CONVERSATION SURVIVES A RESTART (gap G5, lesson 8.5). Every brain gets the checkpointer
   the profile chooses: SqliteSaver on the laptop, PostgresSaver on Cloud SQL in production,
   InMemorySaver only when CHECKPOINT_DSN=memory says so out loud. The thread id is
   `tenant:user:session` - 8.5's boundary - built by the SERVER from the verified identity, so
   a session id from the request can name a conversation but never a tenant. The connection
   is opened once, in the lifespan, and outlives every request: 8.5's trap is returning from
   inside `from_conn_string()`. `setup()` is NOT called here - `migrate.py` is a one-off job.
5. THE BRAIN IS A SWITCH, NOT A FORK (gap G6, lesson 8.7). DOCUMIND_BRAIN picks the default;
   `brain` on the request (allow-listed) overrides it per turn - that is the UI's radio. Brains
   are built lazily and cached for the process; one that is not installed is a 501, not a
   crash at startup. The usage row names the brain, and rag-api's row does too.

Verified 2026-09-04 against langchain 1.4.0 / langchain-google-genai 4.4.0. The bearer leg (SELF_URL)
arrived with Module 8 on the lane, 2026-09-08: it is what lets the chat service run on the lean profile
and be smoke-tested from a shell, and it changes nothing for a person behind IAP.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import ExitStack, asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from brains import BRAINS, DEFAULT_BRAIN, build as build_brain
from shared import iap
from shared.profile import PROFILE
from shared.tenancy import tenant_for

logger = logging.getLogger("documind.chat.agent")

CHECKPOINT_DSN = os.environ.get("CHECKPOINT_DSN", "")
# This service's own URL - the audience an agent, a smoke test or 8.7's notebook mints an ID
# token for when there is no person and no IAP assertion (shared/iap.identity, the bearer leg).
SELF_URL = os.environ.get("SELF_URL", "").rstrip("/")


def build_checkpointer(stack: ExitStack):
    """8.5's three lanes. `stack` keeps the connection open for the life of the process."""
    if PROFILE == "local":
        # A file on disk. Survives a restart of the process - and is per-instance, which is
        # why it is the laptop lane and not a small production one.
        from langgraph.checkpoint.sqlite import SqliteSaver
        path = os.environ.get("DOCUMIND_THREADS_DB", "./documind_threads.db")
        return stack.enter_context(SqliteSaver.from_conn_string(path))
    if CHECKPOINT_DSN == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        logger.warning("CHECKPOINT_DSN=memory: conversations die with the instance (8.5). "
                       "Tests only - never a deployment.")
        return InMemorySaver()
    if not CHECKPOINT_DSN:
        raise RuntimeError("CHECKPOINT_DSN is not set. terraform/cloudsql.tf creates the instance "
                           "and the secret; --set-secrets mounts it (commands/lesson-12.8.sh).")
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # Cloud Run reaches Cloud SQL over a unix socket; the DSN carries host=/cloudsql/... (8.5).
    # autocommit, prepare_threshold=0 and dict_row are what the checkpointer requires of psycopg.
    pool = stack.enter_context(ConnectionPool(
        CHECKPOINT_DSN, min_size=1, max_size=4, open=True,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}))
    return PostgresSaver(pool)      # setup() deliberately absent - see migrate.py


def thread_config(tenant_id: str, user_id: str, session_id: str) -> dict:
    """The thread id IS the tenancy boundary (8.5): tenant first, so nothing after it can forge
    another tenant; ':' banned in every part, so two users cannot collide on one thread."""
    parts = (tenant_id, user_id, session_id)
    if not all(parts):
        raise HTTPException(500, "tenant, user and session ids must all be non-empty")
    if any(":" in p for p in parts):
        raise HTTPException(400, "ids must not contain ':'")
    return {"configurable": {"thread_id": f"{tenant_id}:{user_id}:{session_id}"}}


def brain_for(app: FastAPI, name: str):
    """Build once, keep for the process. A framework that is not installed is a 501."""
    if name not in app.state.brains:
        try:
            app.state.brains[name] = build_brain(name, app.state.checkpointer)
        except ImportError as exc:
            raise HTTPException(501, f"brain {name!r} is not installed in this image: {exc.name}")
    return app.state.brains[name]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the checkpointer once; brains are built on first use and share it."""
    with ExitStack() as stack:
        app.state.checkpointer = build_checkpointer(stack)
        app.state.brains = {}
        yield


app = FastAPI(title="documind-chat", lifespan=lifespan)


class ChatRequest(BaseModel):
    """The question, which conversation it belongs to, and optionally which brain - nothing else.

    tenant_id and user_id used to be fields here, and the agent believed them. Removing them
    is stronger than validating them: a validation is something a later edit can loosen, and
    a field that does not exist is not. There is now nowhere in this request to lie. session_id
    names a conversation INSIDE the caller's own tenant and user - the server prefixes both."""

    question: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    brain: Optional[Literal["langchain", "langgraph", "adk", "direct"]] = None


def caller(request: Request) -> dict:
    """Who is asking, verified - one implementation, shared/iap.py, four surfaces.

    Returns the email AND the raw assertion: the assertion is forwarded to rag-api so the
    retrieval is made in the person's name, not the service's."""
    if PROFILE == "local":
        # No IAP on a laptop. Named, explicit, and unreachable once the profile is gcp.
        return {"email": os.environ.get("LOCAL_USER", "dev@documind.local"), "assertion": None}
    try:
        # The assertion first - a person, through the UI, and it is forwarded to rag-api so the
        # retrieval is made in their name. The bearer token second - an agent, make smoke-chat,
        # 8.7's notebook: minted for THIS service's URL, verified here, and the roster still decides
        # (7.1 taught the leg; 12.8 shipped it on every surface). One implementation: shared/iap.py.
        who = iap.identity(request.headers, bearer_audience=SELF_URL or None)
        return {"email": who["email"],
                "assertion": request.headers.get(iap.ASSERTION_HEADER) if who["via"] == "iap" else None}
    except iap.IapError as e:
        # 401, not 403: we do not know who this is. 403 is for somebody we DO know and
        # will not serve, and the difference matters when you are reading logs at 3am.
        raise HTTPException(401, str(e))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "profile": PROFILE, "brains": list(BRAINS), "default_brain": DEFAULT_BRAIN}


@app.post("/v1/chat")
def chat(req: ChatRequest, request: Request, user=Depends(caller)) -> dict:
    # The tenant is LOOKED UP, never received. Same Firestore roster rag-api checks in
    # enforce_membership() and the frontend reads in tenant_for() - one roster, three
    # surfaces, so a membership change takes effect everywhere at once. The local lane has
    # no roster and no Firestore: LOCAL_TENANT names the one tenant the Chroma directory holds.
    if PROFILE == "local":
        tenant_id = os.environ.get("LOCAL_TENANT", "acme")
    else:
        tenant_id = tenant_for(user["email"])
    if tenant_id is None:
        raise HTTPException(403, "not a member of any tenant")

    name = req.brain or DEFAULT_BRAIN
    brain = brain_for(request.app, name)

    # The tenant, the assertion and the brain travel in the runtime context, NOT in the
    # question and NOT in the tool schema. tools.py reads them from the ToolRuntime the
    # framework injects. The thread id carries the checkpoint (8.5): same tenant, same user,
    # same session -> the conversation continues, on whichever instance answers.
    t0 = time.monotonic()
    out = brain.answer(
        req.question,
        config=thread_config(tenant_id, user["email"], req.session_id),
        context={"tenant_id": tenant_id, "user_id": user["email"],
                 "assertion": user["assertion"] or "", "brain": name},
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    # One row per turn, in the shape 12.3's sink collects: WHICH brain answered is the field
    # 8.7's cost comparison needs and the one the plan's M12 gate asks for.
    logger.info(json.dumps({"event": "chat", "surface": "chat", "brain": name,
                            "tenant": tenant_id, "user": user["email"],
                            "session_id": req.session_id, "latency_ms": latency_ms,
                            "tool_calls": out.get("tool_calls", []),
                            "refusals": out.get("refusals", [])}))
    return {**out, "brain": name, "session_id": req.session_id, "latency_ms": latency_ms}
