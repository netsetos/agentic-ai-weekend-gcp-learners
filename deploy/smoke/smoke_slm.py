#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the self-hosted model on a Cloud Run GPU (11.4). Module 11.

    DOCUMIND_SLM_URL=https://documind-slm-NUMBER.us-central1.run.app \\
    DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com \\
    [DOCUMIND_GATEWAY_URL=https://documind-gateway-NUMBER.us-central1.run.app] \\
    python deploy/smoke/smoke_slm.py                            # or: make smoke-slm PROJECT=...

Checks:
    1. /api/tags lists documind-slm - the model is loaded, not merely the process alive (the startup probe's endpoint)
    2. /api/generate answers (Ollama's own door); the first call after idle is the cold start, timed and printed
    3. /v1/chat/completions answers (the OpenAI-compatible door 11.2's clients and the gateway's inference route use)
    4. through the gateway's documind-slm route, when DOCUMIND_GATEWAY_URL is set - the response names what answered
    5. what the service says it serves: the slm-source label (gguf = 10.5's fine-tune, stock = the stand-in)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SLM = os.environ.get("DOCUMIND_SLM_URL", "").rstrip("/")
GATEWAY = os.environ.get("DOCUMIND_GATEWAY_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
PROJECT = os.environ.get("DOCUMIND_PROJECT", "")
REGION = os.environ.get("SLM_REGION", "us-central1")
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


def call(method: str, url: str, token: str | None, body: dict | None = None, timeout: int = 240):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw), time.time() - t0
            except ValueError:
                return r.status, raw[:300], time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300], time.time() - t0
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


def main() -> int:
    if not SLM:
        print("DOCUMIND_SLM_URL is not set - this is a LIVE test, run it after `make deploy-slm`.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind SLM - live smoke test\n  target: {SLM}\n  " + "-" * 56)
    token = token_as(IMPERSONATE, SLM)
    if not token:
        bad("token", "could not mint an ID token for the SLM's audience"); return 1

    status, body, secs = call("GET", f"{SLM}/api/tags", token)
    names = [m.get("name", "") for m in (body.get("models") or [])] if isinstance(body, dict) else []
    (ok if any(n.startswith("documind-slm") for n in names) else bad)("/api/tags lists documind-slm",
                                                                       f"HTTP {status} in {secs:.1f}s (cold start included): {names}")

    status, body, secs = call("POST", f"{SLM}/api/generate", token,
                              {"model": "documind-slm", "prompt": "Reply with the single word OK.", "stream": False})
    text = body.get("response", "") if isinstance(body, dict) else ""
    (ok if status == 200 and text else bad)("/api/generate answers", f"{text.strip()[:40]!r} in {secs:.1f}s")

    status, body, secs = call("POST", f"{SLM}/v1/chat/completions", token,
                              {"model": "documind-slm", "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
    try:
        text = body["choices"][0]["message"]["content"]
        ok("/v1/chat/completions answers (the OpenAI-compatible door)", f"{text.strip()[:40]!r} in {secs:.1f}s")
    except Exception:  # noqa: BLE001
        bad("/v1/chat/completions answers (the OpenAI-compatible door)", f"HTTP {status}: {str(body)[:200]}")

    if GATEWAY:
        gtoken = token_as(IMPERSONATE, GATEWAY)
        status, body, secs = call("POST", f"{GATEWAY}/v1/chat/completions", gtoken,
                                  {"model": "documind-slm", "messages": [{"role": "user", "content": "Reply with the single word OK."}], "max_tokens": 20})
        served = body.get("model") if isinstance(body, dict) else None
        (ok if status == 200 else bad)("through the gateway's documind-slm route", f"HTTP {status}, served by {served!r} in {secs:.1f}s")

    if PROJECT:
        r = subprocess.run(["gcloud", "run", "services", "describe", "documind-slm", "--region", REGION, "--project", PROJECT,
                            "--format=value(metadata.labels.slm-source,spec.template.spec.containers[0].image)"],
                           capture_output=True, text=True)
        ok("what the service serves", r.stdout.strip() or "(no slm-source label: an older deploy)")

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
