"""DocuMind chat service - three brains over one tool. Lessons 8.1-8.7, gap G6.

8.7's harness put one question through three agent runtimes and printed three cost lines, and
the finding was that every difference was the HARNESS - the tool was identical. This module is
that harness, shipped: the same `tools.py` (which adapts the one `documind_tools.retrieve()`),
the same checkpointer, the same thread id, four ways to drive a model at them:

    langchain   6.4's create_agent + the guard as middleware          (the default)
    langgraph   8.5/8.6's hand-built StateGraph - agent, tools, refuse (more control, nothing done for you)
    adk         8.1-8.4's LlmAgent + Runner, the guard as before_tool_callback
    direct      no loop at all: one retrieve(), rag-api's own grounded answer - the floor to compare against

Which one answers is DOCUMIND_BRAIN (the deploy) or the `brain` field on the request (the UI's
radio, 12.4), allow-listed to these four names. `agent.py` dispatches and logs the brain on
every usage row; `documind_tools.retrieve(brain=)` carries it to rag-api so ITS row has it too.

Two honest limits. The ADK brain keeps its session in ADK's own session service, not the
LangGraph checkpointer - 8.3's Agent Engine sessions are its production answer; here it uses
DatabaseSessionService on the same Cloud SQL when CHECKPOINT_DSN is set, InMemorySessionService
otherwise. And each brain imports its framework lazily, so a deployment that does not install
google-adk simply reports that brain as unavailable (501), rather than failing to start.

Verified offline 2026-09-05: the LangGraph brain's loop, refuse path, context delivery and
checkpoint (tools/check_auth_wiring.py). The ADK brain follows 8.7 cell 12 (google-adk 2.8.0).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os

from langchain_core.messages import SystemMessage, ToolMessage

from shared import documind_tools
from shared.profile import LOCAL_MODEL, PROFILE, build_llm
from tools import BLOCKED, TIMEOUTS, TOOLS

logger = logging.getLogger("documind.chat.brains")

BRAINS = ("langchain", "langgraph", "adk", "direct")
DEFAULT_BRAIN = os.environ.get("DOCUMIND_BRAIN", "langchain")
MODEL = os.environ.get("CHAT_MODEL", "gemini-3.6-flash")

SYSTEM = (
    "You are DocuMind AI, a document intelligence assistant. "
    "Use the tools for document questions. When estimating costs, search first so the page "
    "count is real rather than guessed. If a tool refuses, say so plainly and explain what "
    "approval is needed - never claim the action was taken."
)


def _summary(messages) -> dict:
    """The same three keys from every brain, so agent.py and the UI never branch on the brain."""
    return {
        "answer": messages[-1].content if messages else "",
        "tool_calls": [tc["name"] for m in messages for tc in (getattr(m, "tool_calls", None) or [])],
        "refusals": [m.name for m in messages if isinstance(m, ToolMessage) and m.status == "error"],
    }


# ----------------------------------------------------------------------------- 1. langchain
def _guard_middleware():
    """6.4's guard, as create_agent middleware. Built here so langchain is imported lazily."""
    import time

    from langchain.agents.middleware import AgentMiddleware

    class GuardMiddleware(AgentMiddleware):
        """Refuse blocked operations, time every call, record all of them."""

        def wrap_tool_call(self, request, handler):
            name = request.tool_call["name"]
            if name in BLOCKED:
                logger.warning("refused %s (blocked list)", name)
                return ToolMessage(content=json.dumps({"error": f"{name} requires manual approval"}),
                                   name=name, tool_call_id=request.tool_call["id"], status="error")
            started = time.monotonic()
            try:
                return handler(request)
            finally:
                elapsed = time.monotonic() - started
                budget = TIMEOUTS.get(name, 30)
                (logger.warning if elapsed > budget else logger.info)(
                    "%s took %.2fs (budget %ss)", name, elapsed, budget)

    return GuardMiddleware()


class LangChainBrain:
    name = "langchain"

    def __init__(self, checkpointer, llm=None):
        from langchain.agents import create_agent

        self.agent = create_agent(model=llm or build_llm(), tools=TOOLS, system_prompt=SYSTEM,
                                  middleware=[_guard_middleware()], checkpointer=checkpointer)

    def answer(self, question: str, *, config: dict, context: dict) -> dict:
        out = self.agent.invoke({"messages": [{"role": "user", "content": question}]},
                                config=config, context=context)
        return _summary(out["messages"])


# ----------------------------------------------------------------------------- 2. langgraph
class LangGraphBrain:
    """8.7 cell 20's graph, plus the refuse path 8.6 drew: the harness is edges and nodes."""

    name = "langgraph"

    def __init__(self, checkpointer, llm=None):
        from langgraph.graph import END, START, MessagesState, StateGraph
        from langgraph.prebuilt import ToolNode

        model = (llm or build_llm()).bind_tools(TOOLS)

        def agent(state):
            # The system prompt is prepended per call, never stored - the checkpoint holds the
            # conversation, not the instructions, so a prompt change applies to old threads too.
            return {"messages": [model.invoke([SystemMessage(content=SYSTEM)] + state["messages"])]}

        def route(state):
            calls = getattr(state["messages"][-1], "tool_calls", None) or []
            if not calls:
                return END
            return "refuse" if any(c["name"] in BLOCKED for c in calls) else "tools"

        def refuse(state):
            # A turn that asks for a blocked tool is refused whole: every call in it gets an
            # error ToolMessage, the model reads them and explains. Same seam as GuardMiddleware.
            last = state["messages"][-1]
            return {"messages": [
                ToolMessage(content=json.dumps({"error": f"{c['name']} requires manual approval"}),
                            name=c["name"], tool_call_id=c["id"], status="error")
                for c in last.tool_calls]}

        g = StateGraph(MessagesState, context_schema=dict)
        g.add_node("agent", agent)
        g.add_node("tools", ToolNode(TOOLS))
        g.add_node("refuse", refuse)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", route, {"tools": "tools", "refuse": "refuse", END: END})
        g.add_edge("tools", "agent")
        g.add_edge("refuse", "agent")
        self.graph = g.compile(checkpointer=checkpointer)

    def answer(self, question: str, *, config: dict, context: dict) -> dict:
        out = self.graph.invoke({"messages": [{"role": "user", "content": question}]},
                                config=config, context=context)
        return _summary(out["messages"])


# ----------------------------------------------------------------------------- 3. adk
# The request's tenant and assertion, for ADK's before_tool_callback. A ContextVar, not an
# attribute: the sync endpoint runs in a thread pool, and asyncio.run() copies the context into
# the task it creates, so two concurrent requests cannot see each other's tenant.
_adk_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("documind_adk_ctx", default={})


class AdkBrain:
    name = "adk"

    def __init__(self, checkpointer=None, llm=None):
        from google.adk.agents import LlmAgent
        from google.adk.agents.run_config import RunConfig
        from google.adk.apps import App
        from google.adk.runners import Runner
        from google.adk.tools import FunctionTool

        def guard_tool(tool, args, tool_context):
            """8.7 cell 12: return a dict to REPLACE the call, None to let it through."""
            if tool.name in BLOCKED:
                return {"error": f"{tool.name} is not reachable from a model turn"}
            ctx = _adk_ctx.get()
            args["tenant_id"] = ctx.get("tenant_id", "")        # never the model's choice
            if ctx.get("assertion"):
                args["assertion"] = ctx["assertion"]
            args["brain"] = "adk"
            return None

        if PROFILE == "local":
            from google.adk.models.lite_llm import LiteLlm
            model = LiteLlm(model=f"ollama_chat/{LOCAL_MODEL}")
        else:
            # ADK's Gemini client reads these; global because 3.x generation is not regional.
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
            model = MODEL

        root = LlmAgent(name="documind_adk", model=model, instruction=SYSTEM,
                        tools=[FunctionTool(documind_tools.retrieve),
                               FunctionTool(documind_tools.calculate_processing_cost)],
                        before_tool_callback=guard_tool)
        self.svc = self._sessions()
        self.runner = Runner(app=App(name="documind", root_agent=root), session_service=self.svc)
        self.run_config = RunConfig(max_llm_calls=int(os.environ.get("ADK_MAX_LLM_CALLS", "12")))

    @staticmethod
    def _sessions():
        """ADK keeps its own sessions. On the same Cloud SQL when there is one, in memory otherwise."""
        dsn = os.environ.get("CHECKPOINT_DSN", "")
        if dsn.startswith("postgresql://"):
            try:
                from google.adk.sessions import DatabaseSessionService
                return DatabaseSessionService(db_url=dsn.replace("postgresql://", "postgresql+psycopg://", 1))
            except Exception as exc:                          # noqa: BLE001
                logger.warning("ADK DatabaseSessionService unavailable (%s); using memory", exc)
        from google.adk.sessions import InMemorySessionService
        return InMemorySessionService()

    def answer(self, question: str, *, config: dict, context: dict) -> dict:
        session_id = config["configurable"]["thread_id"]     # tenant:user:session - the same boundary
        user_id = context.get("user_id") or "user"
        token = _adk_ctx.set(context)
        try:
            return asyncio.run(self._run(question, user_id, session_id))
        finally:
            _adk_ctx.reset(token)

    async def _run(self, question: str, user_id: str, session_id: str) -> dict:
        from google.genai import types as gt

        sess = await self.svc.get_session(app_name="documind", user_id=user_id, session_id=session_id)
        if sess is None:
            sess = await self.svc.create_session(app_name="documind", user_id=user_id, session_id=session_id)
        answer, tool_calls, refusals = "", [], []
        async for ev in self.runner.run_async(
                user_id=user_id, session_id=sess.id,
                new_message=gt.Content(role="user", parts=[gt.Part(text=question)]),
                run_config=self.run_config):
            for call in (ev.get_function_calls() or []):
                tool_calls.append(call.name)
                if call.name in BLOCKED:
                    refusals.append(call.name)
            if ev.is_final_response() and ev.content and ev.content.parts:
                answer = "".join(p.text or "" for p in ev.content.parts)
        return {"answer": answer, "tool_calls": tool_calls, "refusals": refusals}


# ----------------------------------------------------------------------------- 4. direct
class DirectBrain:
    """No loop. One retrieve(), and rag-api's own grounded answer comes back with it - the
    cheapest brain, and the one the other three have to beat to justify their harness."""

    name = "direct"

    def __init__(self, checkpointer=None, llm=None):
        self.llm = llm

    def answer(self, question: str, *, config: dict, context: dict) -> dict:
        r = documind_tools.retrieve(question, tenant_id=context.get("tenant_id", ""), top_k=5,
                                    assertion=context.get("assertion") or None, brain="direct")
        if "error" in r:
            return {"answer": r["error"], "tool_calls": ["retrieve"], "refusals": [], "citations": []}
        if r.get("answer"):
            return {"answer": r["answer"], "tool_calls": ["retrieve"], "refusals": [],
                    "citations": r["citations"]}
        # The local lane has no generator behind retrieve(): one grounded call to the profile's model.
        ctx = "\n".join(f"[Source {i}] {c['quote']}" for i, c in enumerate(r["citations"], 1))
        msg = (self.llm or build_llm()).invoke(
            f"{SYSTEM}\nAnswer only from the context and cite [Source N].\n\nContext:\n{ctx}\n\nQuestion: {question}")
        return {"answer": getattr(msg, "content", str(msg)), "tool_calls": ["retrieve"],
                "refusals": [], "citations": r["citations"]}


_REGISTRY = {"langchain": LangChainBrain, "langgraph": LangGraphBrain, "adk": AdkBrain, "direct": DirectBrain}


def build(name: str, checkpointer, llm=None):
    """One brain, by name. ImportError means that framework is not installed in this image."""
    if name not in BRAINS:
        raise ValueError(f"unknown brain {name!r}; one of {BRAINS}")
    return _REGISTRY[name](checkpointer, llm)
