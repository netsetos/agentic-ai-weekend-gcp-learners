#!/usr/bin/env python3
"""Live smoke test for the chat service (lessons 8.7 and 12.8) - four brains over one tool.

    make smoke-chat PROJECT=...                     # from deploy/, after documind-chat is deployed
    DOCUMIND_CHAT_URL=https://documind-chat-NUMBER.us-central1.run.app \\
      DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com python smoke/smoke_chat.py

    1. GET /health                    -> 200 and the four brain names
    2. POST /v1/chat, brain=direct    -> an answer with citations, as a roster member
    3. POST /v1/chat x langchain, langgraph, adk -> an answer, and retrieve() among the tool calls
    4. POST /v1/chat as the outsider  -> 403 from the roster, not 401 from the verifier
                                         (optional: DOCUMIND_OUTSIDER_SA, the eval gate's identity)

The identity is a Google ID token minted AS a service account with the email inside, the
audience being THIS service's URL (shared/iap.identity's bearer leg, 12.8). The chat service
looks the tenant up from the roster - there is no tenant field to send, which is the point.
Each agent brain is a cold import the first time (langchain, langgraph, google-adk), so the
timeout is generous and the first call is the slow one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CHAT_URL = os.environ.get("DOCUMIND_CHAT_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
OUTSIDER = os.environ.get("DOCUMIND_OUTSIDER_SA", "")
QUESTION = os.environ.get("DOCUMIND_SMOKE_QUESTION",
                          "After how many years of continuous service does gratuity become payable?")
BRAINS = ("direct", "langchain", "langgraph", "adk")
passed, failed = [], []


def ok(name, detail=""):
    passed.append(name); print(f"  [PASS] {name}  {detail}")


def bad(name, detail=""):
    failed.append(name); print(f"  [FAIL] {name}  {detail}")


def token_as(sa: str) -> str | None:
    if not sa:
        return None
    try:
        out = subprocess.run(["gcloud", "auth", "print-identity-token", "--include-email",
                              f"--impersonate-service-account={sa}", f"--audiences={CHAT_URL}"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not mint a token as {sa}: {getattr(e, 'stderr', '') or e})".strip()[:300])
        return None


def call(path: str, token: str | None, body: dict | None = None, timeout: int = 180):
    """(status, json-or-text). Errors come back as data so a 403 is a result, not a crash."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{CHAT_URL}{path}", data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:300]


def main() -> int:
    if not CHAT_URL:
        print("DOCUMIND_CHAT_URL is not set - this is a LIVE test, run it after documind-chat is deployed.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind chat - live smoke test\n  target: {CHAT_URL}\n  " + "-" * 56)
    member = token_as(IMPERSONATE)
    if IMPERSONATE and not member:
        print("  (proceeding unauthenticated - expect refusals on a private service)")

    # 1. health: the brains the image can build
    status, body = call("/health", member)
    if status == 200 and isinstance(body, dict) and set(BRAINS) <= set(body.get("brains", [])):
        ok("health", f"profile={body.get('profile')} default={body.get('default_brain')}")
    else:
        bad("health", f"status={status} body={str(body)[:120]}")

    # 2-3. one question, four brains. retrieve() must be among the tool calls of every brain:
    # an agent that answered without it answered from memory (7.3's gate, live).
    for brain in BRAINS:
        status, body = call("/v1/chat", member, {"question": QUESTION, "session_id": f"smoke-{brain}", "brain": brain})
        if status != 200 or not isinstance(body, dict):
            bad(f"brain {brain}", f"status={status} body={str(body)[:160]}")
            continue
        answer, tools = str(body.get("answer") or ""), body.get("tool_calls") or []
        retrieved = any("retrieve" in t for t in tools)
        # The first live run passed on an answer that SAID retrieval was unavailable: the tool was
        # called and the model apologised. A smoke that cannot tell that from a cited answer is not
        # one. The direct brain returns the lane's citations - require them; every brain's answer
        # must not carry the tool layer's own failure text.
        grounded = "unavailable" not in answer.lower()
        if brain == "direct":
            grounded = grounded and bool(body.get("citations"))
        if answer and retrieved and grounded and body.get("brain") == brain:
            ok(f"brain {brain}", f"{body.get('latency_ms')} ms  tools={tools}  {answer[:60]!r}")
        else:
            bad(f"brain {brain}", f"grounded={grounded} answer={answer[:70]!r} tools={tools} brain={body.get('brain')}")

    # 4. the outsider: invited by IAM, refused by the roster - 403, and not 401
    if OUTSIDER:
        outsider = token_as(OUTSIDER)
        status, body = call("/v1/chat", outsider, {"question": QUESTION, "session_id": "smoke-outsider"})
        detail = str(body.get("detail") if isinstance(body, dict) else body)[:100]
        (ok if status == 403 else bad)("outsider refused", f"status={status} {detail}")

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
