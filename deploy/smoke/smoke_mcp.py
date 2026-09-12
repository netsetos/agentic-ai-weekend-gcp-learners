#!/usr/bin/env python3
"""Live smoke test for the MCP server (lesson 7.2) - the lane's agent surface, four checks.

    make smoke-mcp PROJECT=...                       # from deploy/, after the mcp service is deployed
    DOCUMIND_MCP_URL=https://documind-mcp-NUMBER.us-central1.run.app \\
      DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com python smoke/smoke_mcp.py

    1. GET /health                       -> 200, the platform's view
    2. tools/list                        -> the four tools, by name
    3. retrieve(the gratuity question)   -> answerable with citations, as a roster member
    4. retrieve as the outsider account  -> refused by the roster, not by the network (optional:
                                            set DOCUMIND_OUTSIDER_SA, the eval gate's identity)

Like smoke/smoke.py it mints the caller's identity with gcloud: --impersonate-service-account,
--audiences=THIS service's URL (Cloud Run checks the token was minted for it; the server checks
it again with SELF_URL), --include-email (the roster looks the caller up by email). Needs
`pip install --user fastmcp==3.4.7` in the shell that runs it.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.request

MCP_URL = os.environ.get("DOCUMIND_MCP_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
OUTSIDER = os.environ.get("DOCUMIND_OUTSIDER_SA", "")
TENANT = os.environ.get("TENANT", "acme")
QUESTION = os.environ.get("DOCUMIND_SMOKE_QUESTION",
                          "After how many years of continuous service does gratuity become payable?")
TOOLS = {"retrieve", "list_documents", "corpus_stats", "calculate_processing_cost"}
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
                              f"--impersonate-service-account={sa}", f"--audiences={MCP_URL}"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  (could not mint a token as {sa}: {getattr(e, 'stderr', '') or e})".strip()[:300])
        return None


async def call(token: str | None, name: str, args: dict):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with Client(StreamableHttpTransport(f"{MCP_URL}/mcp", headers=headers)) as c:
        if name == "__list__":
            return [t.name for t in await c.list_tools()]
        r = await c.call_tool(name, args)
        return r.data if getattr(r, "data", None) is not None else r


def main() -> int:
    if not MCP_URL:
        print("DOCUMIND_MCP_URL is not set - this is a LIVE test, run it after the mcp service is deployed.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind MCP - live smoke test\n  target: {MCP_URL}\n  " + "-" * 56)
    member = token_as(IMPERSONATE)
    if IMPERSONATE and not member:
        print("  (proceeding unauthenticated - expect refusals on a private service)")

    # 1. health, through the platform's IAM ingress
    req = urllib.request.Request(f"{MCP_URL}/health")
    if member:
        req.add_header("Authorization", f"Bearer {member}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            ok("health", body[:80]) if r.status == 200 else bad("health", f"status={r.status}")
    except Exception as e:  # noqa: BLE001
        bad("health", str(e)[:120])

    # 2. the four tools
    try:
        names = set(asyncio.run(call(member, "__list__", {})))
        (ok if TOOLS <= names else bad)("tools/list", f"{sorted(names)}")
    except Exception as e:  # noqa: BLE001
        bad("tools/list", f"{type(e).__name__}: {str(e)[:160]}")

    # 3. a question the corpus answers, as a roster member
    try:
        out = asyncio.run(call(member, "retrieve", {"query": QUESTION, "tenant": TENANT, "top_k": 5}))
        if isinstance(out, str):
            out = json.loads(out)
        cites = len(out.get("citations") or [])
        if out.get("answerable") and cites:
            ok("retrieve", f"answerable=True citations={cites}  {(out.get('answer') or '')[:70]!r}")
        else:
            bad("retrieve", f"answerable={out.get('answerable')} citations={cites} error={out.get('error')}")
    except Exception as e:  # noqa: BLE001
        bad("retrieve", f"{type(e).__name__}: {str(e)[:200]}")

    # 4. the outsider: invited by IAM, refused by the roster
    if OUTSIDER:
        outsider = token_as(OUTSIDER)
        try:
            out = asyncio.run(call(outsider, "retrieve", {"query": QUESTION, "tenant": TENANT}))
            bad("outsider refused", f"answered: {str(out)[:120]}")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            (ok if "roster" in msg or "not authenticated" in msg else bad)("outsider refused", msg[:160])

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
