"""DocuMind chat - a thin client over rag-api.

There is no model call in this file and there must never be one. Retrieval,
grounding, citations, the prompt and the cost accounting all live in rag-api;
the UI streams what it is told and renders it. The version this replaced
generated through LiteLLM, imported answer_query and render_with_citations,
and used neither - a plain chatbot wearing a RAG product's name.
"""
import json
import os
import uuid

import requests
import streamlit as st
import google.auth.transport.requests
import google.oauth2.id_token

from citations import render_with_citations
from auth import tenant_for
from voice import cached_tts, transcribe

RAG_API_URL = os.environ["RAG_API_URL"]
# The chat SERVICE (12.8). When set, the page reaches it: three agent brains over the one
# retrieve() (8.7, gap G6), chosen per turn from the sidebar, plus a link to open the surface
# itself (gap G13). Unset - the local profile, or no chat deployed - and this page stays what
# it always was: a thin streaming client over rag-api, which is the "direct" brain.
CHAT_URL = os.environ.get("CHAT_URL", "").rstrip("/")
BRAINS = ("direct", "langchain", "langgraph", "adk")
USD_INR = 85
# gemini-3.6-flash standard rates, USD per 1M tokens. The $0.75/$3.75
# introductory price runs to 31 Dec 2026.
PRICE_IN, PRICE_OUT = 1.50, 7.50


def _id_token(audience: str) -> str:
    """Minted per call - a Cloud Run ID token lasts an hour (lesson 7.3)."""
    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience)


def _headers(audience: str = RAG_API_URL) -> dict:
    """TWO credentials, two different gates.

    Authorization: the frontend's own ID token, audience = the service URL being called
    (rag-api, or the chat service). This gets past Cloud Run IAM, and is why ui-sa needs
    roles/run.invoker.

    x-goog-iap-jwt-assertion: the END USER's assertion, forwarded unchanged so the
    callee's shared/iap.py sees the person rather than this service account. Without
    it every request would look like it came from the frontend, and per-user
    audit and per-tenant membership would both be meaningless.
    """
    h = {"Authorization": f"Bearer {_id_token(audience)}"}
    assertion = (st.context.headers or {}).get("x-goog-iap-jwt-assertion")
    if assertion:
        h["x-goog-iap-jwt-assertion"] = assertion
    return h


def stream_answer(query: str, tenant_id: str):
    """Yield (event_name, payload) from rag-api's SSE: citation -> token -> done."""
    with requests.post(
        f"{RAG_API_URL}/v1/stream",
        json={"query": query, "tenant_id": tenant_id, "top_k": 5, "brain": "ui"},
        headers=_headers(), stream=True, timeout=120,
    ) as resp:
        resp.raise_for_status()
        event = None
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                yield event, json.loads(line[6:])


def chat_page(user):
    tenant_id = tenant_for(user["email"])
    if not tenant_id:
        st.error("Your account is not a member of any DocuMind tenant. "
                 "Ask an administrator to add you.")
        st.stop()

    with st.sidebar:
        st.caption(f"Tenant: **{tenant_id}**")
        if "session_id" not in st.session_state:
            st.session_state.session_id = uuid.uuid4().hex[:12]   # one conversation per browser session
        if CHAT_URL:
            # 8.7 in production (gap G6): the same question, any of the three agent brains or
            # the direct path, all over the one retrieve(). The chat service logs which one.
            brain = st.radio("Brain", BRAINS, index=0,
                             help="direct = this page streaming from rag-api; the others run "
                                  "in documind-chat with tools, a guard and a checkpointer")
            st.link_button("Open DocuMind Chat", CHAT_URL)   # the surface itself (gap G13)
        else:
            brain = "direct"
        # No temperature or top_p. gemini-3.6-flash ignores temperature, top_p
        # and top_k, so a slider here would be a control that does nothing -
        # worse than no control, because people tune it and believe the result.
        # 9.2, wired: Chirp 3 HD reads the answer back, cached in the TTS bucket by the SHA-256
        # of voice and text, so the second time a figure is read out costs nothing.
        st.session_state.read_aloud = st.toggle("Read answers aloud", value=bool(st.session_state.get("read_aloud")),
                                                help="Chirp 3 HD (en-IN) via voice.cached_tts; free on a cache hit")
        st.divider()
        st.subheader("Cost this session")
        ss = st.session_state
        st.metric("USD", f"${ss.get('session_cost_usd', 0):.4f}",
                  delta=f"+${ss.get('last_turn_cost', 0):.4f} last turn")
        st.metric("INR", f"Rs {ss.get('session_cost_usd', 0) * USD_INR:.2f}")
        st.metric("Tokens", f"{ss.get('tokens_in', 0)} in / "
                            f"{ss.get('tokens_out', 0)} out")

    st.title("DocuMind Chat")
    if "messages" not in st.session_state:
        st.session_state.messages = []
    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    spoken = transcribe_from_mic()
    typed = st.chat_input("Ask DocuMind...", max_chars=4000)
    prompt = spoken or typed
    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    if brain != "direct":
        # An AGENT brain: the chat service runs the loop (tools, guard, checkpointer) and this
        # page renders what it returns. session_id keeps one conversation per browser session,
        # inside this user's own tenant - the service prefixes both from the verified identity.
        with st.chat_message("assistant"):
            with st.spinner(f"{brain} is thinking..."):
                r = requests.post(f"{CHAT_URL}/v1/chat",
                                  json={"question": prompt, "session_id": st.session_state.session_id,
                                        "brain": brain},
                                  headers=_headers(CHAT_URL), timeout=120)
            if r.status_code != 200:
                st.error(f"chat service returned {r.status_code}: {r.text[:200]}")
                return
            body = r.json()
            answer = body.get("answer", "")
            answer = answer if isinstance(answer, str) else str(answer)
            st.markdown(answer)
            if st.session_state.get("read_aloud") and answer:
                st.audio(cached_tts(answer[:1500]), format="audio/ogg")
            st.caption(f"brain: {body.get('brain')} · tools: {', '.join(body.get('tool_calls') or []) or 'none'}"
                       + (f" · refused: {', '.join(body['refusals'])}" if body.get("refusals") else "")
                       + f" · {body.get('latency_ms', 0)} ms")
        st.session_state.messages.append({"role": "assistant", "content": answer})
        return

    with st.chat_message("assistant"):
        sources, answer, done = [], "", {}
        saw_done = False
        slot = st.empty()
        try:
            for event, payload in stream_answer(prompt, tenant_id):
                if event == "citation":
                    # Citations arrive BEFORE the first token - they come from
                    # retrieval, so the sources render while the answer is written.
                    # The five fields a citation always had, plus 9.6's four: a figure or a
                    # video segment arrives here with a locator, and citations.py renders it.
                    # Drop these keys and a figure comes back as plain text with no thumbnail,
                    # nothing raised, nothing logged - the silent projection 9.6 warned about.
                    sources.append({"text": payload.get("quote", ""),
                                    "source_uri": payload["source"],
                                    "page_start": payload.get("page"),
                                    "kind": payload.get("kind", "text"),
                                    "media_url": payload.get("media_url"),
                                    "start": payload.get("start"),
                                    "end": payload.get("end"),
                                    "effective_from": payload.get("effective_from")})
                elif event == "token":
                    answer += payload["t"]
                    slot.markdown(answer)
                elif event == "done":
                    done = payload
                    saw_done = True
                elif event == "error":
                    slot.empty()
                    st.error("The answer stream failed. No completed answer was recorded.")
                    return
        except (requests.RequestException, ValueError, KeyError) as exc:
            slot.empty()
            st.error(f"The answer stream failed: {type(exc).__name__}. Retry after checking the API.")
            return
        if not saw_done or not answer.strip():
            slot.empty()
            st.error("The stream ended without a completed answer. Check the API and retry.")
            return
        slot.empty()
        render_with_citations(answer, sources)
        if st.session_state.get("read_aloud") and answer:
            st.audio(cached_tts(answer[:1500]), format="audio/ogg")

    cost = (done.get("tokens_in", 0) * PRICE_IN
            + done.get("tokens_out", 0) * PRICE_OUT) / 1_000_000
    ss = st.session_state
    ss.session_cost_usd = ss.get("session_cost_usd", 0) + cost
    ss.last_turn_cost = cost
    ss.tokens_in = ss.get("tokens_in", 0) + done.get("tokens_in", 0)
    ss.tokens_out = ss.get("tokens_out", 0) + done.get("tokens_out", 0)
    st.session_state.messages.append({"role": "assistant", "content": answer})


def transcribe_from_mic() -> str:
    """voice.py, wired. It shipped in the image and no page imported it."""
    try:
        from streamlit_mic_recorder import mic_recorder
    except ImportError:
        return ""
    audio = mic_recorder(start_prompt="Speak", stop_prompt="Stop",
                         just_once=True, key="documind-mic")
    if not audio or not audio.get("bytes"):
        return ""
    return transcribe(audio["bytes"])
