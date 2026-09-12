#!/usr/bin/env python3
"""Live smoke test for the A2A peer (lesson 8.4) - three protocols in one request, four checks.

    make smoke-agent PROJECT=...                      # from deploy/, after documind-agent is deployed
    DOCUMIND_AGENT_URL=https://documind-agent-NUMBER.us-central1.run.app \\
      DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com python smoke/smoke_agent.py

    1. the agent card WITHOUT a token   -> refused by Cloud Run IAM (401/403): the peer is private
    2. the agent card with a token      -> 200, and the card's address is this service's url
    3. message/send (the gratuity row)  -> a completed task whose answer came through documind-mcp
    4. message/send naming tenant zeta  -> the roster's refusal, relayed by the peer (its account is on acme only)

Plain urllib and JSON-RPC - no a2a-sdk in the shell. The message shape is the one the ADK-served
A2A 1.x endpoint accepts (proven offline against a2a-sdk 1.1.2 + google-adk 2.8.0, 8 Sept 2026):
    {"role": "user", "kind": "message", "messageId": ..., "parts": [{"kind": "text", "text": ...}]}
The identity is the CALLER's ID token, minted for THIS service's url; the peer speaks to the lane
as documind-agent-sa - A2A carries no identity of its own, which is what check 4 demonstrates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

AGENT_URL = os.environ.get("DOCUMIND_AGENT_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
QUESTION = os.environ.get("DOCUMIND_SMOKE_QUESTION",
                          "After how many years of continuous service does gratuity become payable?")
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
                              f"--impersonate-service-account={sa}", f"--audiences={AGENT_URL}"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not mint a token as {sa}: {getattr(e, 'stderr', '') or e})".strip()[:300])
        return None


def http(path: str, token: str | None, body: dict | None = None, timeout: int = 180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{AGENT_URL}{path}", data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:300]


def send(token: str | None, text: str):
    """message/send -> (status, task-or-error, the agent's text)."""
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
            "params": {"message": {"role": "user", "kind": "message", "messageId": str(uuid.uuid4()),
                                   "parts": [{"kind": "text", "text": text}]}}}
    status, j = http("/", token, body)
    result = j.get("result", {}) if isinstance(j, dict) else {}
    texts = [p.get("text", "") for a in result.get("artifacts", []) for p in a.get("parts", []) if p.get("kind") == "text"]
    if not texts:   # a Message result, or a task whose answer sits in the history
        for m in ([result] if result.get("kind") == "message" else result.get("history", []))[::-1]:
            if m.get("role") == "agent":
                texts = [p.get("text", "") for p in m.get("parts", []) if p.get("kind") == "text"]
                if texts:
                    break
    return status, j, "\n".join(t for t in texts if t)


def main() -> int:
    if not AGENT_URL:
        print("DOCUMIND_AGENT_URL is not set - this is a LIVE test, run it after documind-agent is deployed.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind A2A peer - live smoke test\n  target: {AGENT_URL}\n  " + "-" * 56)
    card_path = "/.well-known/agent-card.json"

    # 1. private: the card is behind IAM
    status, _ = http(card_path, None)
    (ok if status in (401, 403) else bad)("card refused without a token", f"status={status}")

    # 2. the card, as a caller IAM admits; its address is this service
    member = token_as(IMPERSONATE)
    status, card = http(card_path, member)
    if status == 200 and isinstance(card, dict):
        ifaces = card.get("supportedInterfaces") or card.get("supported_interfaces") or []
        addr = card.get("url") or (ifaces[0].get("url") if ifaces else "")
        skills = [s.get("id") for s in card.get("skills", [])]
        (ok if AGENT_URL.split("//", 1)[1] in str(addr) else bad)("agent card", f"name={card.get('name')} url={addr} skills={skills}")
    else:
        bad("agent card", f"status={status} {str(card)[:120]}")

    # 3. one task, answered through the MCP server
    status, j, text = send(member, QUESTION)
    state = (j.get("result", {}) if isinstance(j, dict) else {}).get("status", {}).get("state")
    if status == 200 and text and ("five" in text.lower() or "5 years" in text.lower()):
        ok("task answered", f"state={state}  {text[:90]!r}")
    else:
        bad("task answered", f"status={status} state={state} text={text[:120]!r} err={(j.get('error') if isinstance(j, dict) else j)}")

    # 4. the roster refusal, relayed: the peer's account is on acme only
    status, j, text = send(member, f"For tenant zeta: {QUESTION}")
    low = text.lower()
    (ok if status == 200 and any(w in low for w in ("roster", "not on", "refus", "cannot", "not a member", "no access")) else bad)(
        "zeta refused by the roster", f"{text[:110]!r}")

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
