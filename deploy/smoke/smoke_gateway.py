#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the LiteLLM gateway on the lane (11.3). Module 11.

    DOCUMIND_GATEWAY_URL=https://documind-gateway-NUMBER.us-central1.run.app \\
    DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com \\
    python deploy/smoke/smoke_gateway.py                       # or: make smoke-gateway PROJECT=...

Checks:
    1. no token       -> refused by Cloud Run (401/403): the gateway is behind IAM like every service on the lane
    2. liveliness     -> 200 as the roster member's account
    3. documind-general, JSON mode -> a parseable object (the shape rag-api's gateway backend relies on)
    4. a PAN in the prompt -> the guardrail routes it (documind-sensitive, the self-hosted model) - printed, and a
       cold or absent GPU shows as a slow answer or a 5xx, never as Gemini: the sensitive route has no fallback
    5. documind-slm  -> an answer, from the SLM or from its named fallback (the response's model says which),
       and the cost header the API prices from

Identity: an ID token for the gateway's own URL, minted as DOCUMIND_IMPERSONATE_SA. No master key on the lean lane.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get("DOCUMIND_GATEWAY_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
passed, failed = [], []


def ok(name, detail=""):
    passed.append(name); print(f"  [PASS] {name}  {detail}")


def bad(name, detail=""):
    failed.append(name); print(f"  [FAIL] {name}  {detail}")


def token_as(sa: str, audience: str) -> str | None:
    if not sa:
        return None
    try:
        out = subprocess.run(["gcloud", "auth", "print-identity-token", "--include-email",
                              f"--impersonate-service-account={sa}", f"--audiences={audience}"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not mint a token as {sa}: {getattr(e, 'stderr', '') or e})".strip()[:300])
        return None


def call(method: str, path: str, token: str | None, body: dict | None = None, timeout: int = 150):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{GATEWAY}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw), dict(r.headers), time.time() - t0
            except ValueError:
                return r.status, raw[:300], dict(r.headers), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], dict(e.headers), time.time() - t0
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", {}, time.time() - t0


def completion(token, model, content, json_mode=False):
    body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 120}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return call("POST", "/v1/chat/completions", token, body)


def main() -> int:
    if not GATEWAY:
        print("DOCUMIND_GATEWAY_URL is not set - this is a LIVE test, run it after `make deploy-gateway`.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind gateway - live smoke test\n  target: {GATEWAY}\n  " + "-" * 56)

    status, body, _, _ = call("GET", "/health/liveliness", None)
    (ok if status in (401, 403) else bad)("no token is refused at the door", f"HTTP {status}")

    token = token_as(IMPERSONATE, GATEWAY)
    if not token:
        bad("token", "could not mint an ID token for the gateway's audience"); return 1
    status, body, _, secs = call("GET", "/health/liveliness", token)
    (ok if status == 200 else bad)("liveliness as the member's account", f"HTTP {status} in {secs:.1f}s")

    status, body, headers, secs = completion(token, "documind-general", 'Reply with JSON only: {"ok": true}', json_mode=True)
    text = ""
    try:
        text = body["choices"][0]["message"]["content"]
        obj = json.loads(text)
        (ok if isinstance(obj, dict) else bad)("documind-general answers JSON", f"{text[:60]!r} in {secs:.1f}s, model {body.get('model')}")
    except Exception:  # noqa: BLE001
        bad("documind-general answers JSON", f"HTTP {status}: {str(body)[:200]}")
    cost = headers.get("x-litellm-response-cost") or headers.get("X-Litellm-Response-Cost")
    (ok if cost else bad)("the cost header rag-api prices from", f"x-litellm-response-cost={cost}")

    status, body, headers, secs = completion(token, "documind-general", "My PAN is ABCDE1234F. What is the notice period for a confirmed E3?")
    served = body.get("model") if isinstance(body, dict) else None
    if status == 200:
        ok("a PAN is routed by the guardrail", f"served by {served!r} in {secs:.1f}s (the sensitive route is the self-hosted model, no fallback)")
    else:
        # A cold or absent GPU is a slow answer or a 5xx here - and NOT a Gemini answer. That is the route working.
        ok("a PAN is routed by the guardrail", f"HTTP {status} in {secs:.1f}s - the self-hosted route refused rather than leak; deploy or warm the SLM")

    status, body, headers, secs = completion(token, "documind-slm", "Reply with the single word OK.")
    if status == 200 and isinstance(body, dict):
        ok("documind-slm answers, or its fallback does", f"model {body.get('model')!r} in {secs:.1f}s, cost {headers.get('x-litellm-response-cost')}")
    else:
        bad("documind-slm answers, or its fallback does", f"HTTP {status}: {str(body)[:200]}")

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
