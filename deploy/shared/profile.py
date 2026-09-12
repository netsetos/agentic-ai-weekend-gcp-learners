"""One switch, two lanes. Lesson 6.4, step 7 - the file its notebook cell became.

    DOCUMIND_PROFILE=gcp     Gemini on Vertex AI + the shared index       (what production runs)
    DOCUMIND_PROFILE=local   Ollama gemma3:4b + a Chroma directory        (what you develop against)

The point is NOT that a local model is as good. It is not. The point is that a broken tool loop,
a bad prompt or a wrong schema can be found on a train with no signal, and that a new engineer
can run the thing on their first morning without a billing account - and, since 2026-09-05
(gap G3), that the DEPLOYED chat service runs on the same switch: `make chat-local` starts it
against this file's local lane with no cloud credential, which is also 13.2's Rs 0 lane.

The contract that makes this worth having: the agent code that calls these two builders is
identical in both lanes. The day a profile needs its own agent code, the switch has failed and
you are maintaining two applications under one name.
"""
from __future__ import annotations

import os

PROFILE = os.environ.get("DOCUMIND_PROFILE", "gcp")
LOCAL_MODEL = os.environ.get("DOCUMIND_LOCAL_MODEL", "gemma3:4b")
CHROMA_DIR = os.environ.get("DOCUMIND_CHROMA_DIR", "./documind_chroma")
CHROMA_COLLECTION = "documind_dev"


def build_llm():
    if PROFILE == "local":
        # Ollama, a model small enough for a laptop. Tool calling on a 4B model is noticeably
        # worse than Gemini - it will sometimes call the wrong tool. That is a feature here:
        # the local lane is where you find out your tool DESCRIPTIONS are ambiguous, because a
        # weaker model is a harsher reader of them.
        from langchain_ollama import ChatOllama
        return ChatOllama(model=LOCAL_MODEL, temperature=0)
    from langchain_google_genai import ChatGoogleGenerativeAI
    # project= is required: without it the constructor resolves Application Default Credentials
    # immediately and raises DefaultCredentialsError on this line rather than at first request.
    # location="global" because Gemini 3.x generation is not served regionally.
    return ChatGoogleGenerativeAI(model=os.environ.get("CHAT_MODEL", "gemini-3.6-flash"),
                                  vertexai=True, project=os.environ["GOOGLE_CLOUD_PROJECT"],
                                  location="global", thinking_level="low")


def build_store(embeddings=None):
    if PROFILE == "local":
        from langchain_chroma import Chroma
        # DeterministicFakeEmbedding, not FakeEmbeddings: the deterministic one returns the
        # same vector for the same text, so a store you write and then query gives stable
        # results. Both live in langchain_core, which is already a dependency - reaching for
        # langchain_community here would pull a package the service never installs. A fake
        # embedding ranks ARBITRARILY, which is why documind_tools' local lane also applies the
        # lexical gate 6.4's mock used; pass OllamaEmbeddings(model="nomic-embed-text") for
        # real semantic ranking, still with no cloud credential.
        from langchain_core.embeddings import DeterministicFakeEmbedding
        return Chroma(collection_name=CHROMA_COLLECTION,
                      embedding_function=embeddings or DeterministicFakeEmbedding(size=768),
                      persist_directory=CHROMA_DIR)
    raise NotImplementedError("the gcp profile retrieves through rag-api - documind_tools.retrieve()")
