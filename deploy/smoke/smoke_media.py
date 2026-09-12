#!/usr/bin/env python3
"""Live smoke test for Module 9 on the lane - media is a document. Five checks.

    make smoke-media PROJECT=...                     # from deploy/, after make media + make ingest-corpus
    DOCUMIND_API_URL=https://documind-api-NUMBER.us-central1.run.app \\
    DOCUMIND_MCP_URL=https://documind-mcp-NUMBER.us-central1.run.app \\
    DOCUMIND_IMPERSONATE_SA=documind-ui-sa@PROJECT.iam.gserviceaccount.com \\
    DOCUMIND_OUTSIDER_SA=documind-outsider-sa@PROJECT.iam.gserviceaccount.com python smoke/smoke_media.py

    1. POST /v1/media/generate as a roster member  -> 200, a blob in the media bucket (9.4's Studio,
                                                      roster-checked, audited, metered; cached under DEMO_MODE)
    2. the same as the outsider account            -> refused by the roster (403), not by the network
    3. POST /v1/query, the Figure 3 question       -> answerable, and a citation with kind=figure and a media_url
                                                      (9.6: the caption is the quote, the asset rides beside it)
    4. list_documents through documind-mcp         -> figure / segment documents in acme's corpus (needs fastmcp)
    5. /v1/media/upload-url + a signed PUT         -> 200 from GCS; then the worker indexes it - polled through
                                                      list_documents for up to 150 s

Check 3 and 4 fail with a plain sentence when the media is not in the corpus yet: run `make media`
and `make ingest-corpus` first. Identity is minted with gcloud the way smoke_mcp.py mints it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("DOCUMIND_API_URL", "").rstrip("/")
MCP_URL = os.environ.get("DOCUMIND_MCP_URL", "").rstrip("/")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA", "")
OUTSIDER = os.environ.get("DOCUMIND_OUTSIDER_SA", "")
TENANT = os.environ.get("TENANT", "acme")
FIGURE_QUESTION = "In Figure 3 of the annual report, which region's revenue declined between FY2025 and FY2026?"
PROMPT = "A clean teal bar chart titled 'smoke test': three bars labelled A, B and C with values 1, 2 and 3."
# A 1x1 white PNG, the same bytes every run: the worker's claim key is the content hash, so the
# second run is a duplicate it acks, and the poll finds the document already indexed.
PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")
PROBE_NAME = "smoke_media_probe.png"
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


def call(method: str, url: str, token: str | None, body: dict | None = None, raw: bytes | None = None,
         content_type: str = "application/json", timeout: int = 120):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, text
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


async def mcp_list(token: str | None, status: str = "indexed"):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with Client(StreamableHttpTransport(f"{MCP_URL}/mcp", headers=headers)) as c:
        r = await c.call_tool("list_documents", {"status": status, "tenant": TENANT})
        out = r.data if getattr(r, "data", None) is not None else r
        if isinstance(out, str):
            out = json.loads(out)
        return out.get("documents") or []


def main() -> int:
    if not API:
        print("DOCUMIND_API_URL is not set - this is a LIVE test, run it after make up / make media / make ingest-corpus.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind Module 9 - live smoke test\n  api: {API}\n  mcp: {MCP_URL or '(unset: MCP legs skipped)'}\n  " + "-" * 56)
    member = token_as(IMPERSONATE, API)

    # 1. generate, as a roster member
    status, body = call("POST", f"{API}/v1/media/generate", member, {"prompt": PROMPT, "tenant_id": TENANT})
    if status == 200 and isinstance(body, dict) and body.get("blob"):
        ok("generate", f"blob={body['blob']} cached={body.get('cached')}")
    else:
        bad("generate", f"status={status} body={str(body)[:160]}")

    # 2. the outsider: IAM admits it, the roster refuses it
    if OUTSIDER:
        status, body = call("POST", f"{API}/v1/media/generate", token_as(OUTSIDER, API),
                            {"prompt": PROMPT, "tenant_id": TENANT})
        (ok if status in (401, 403) else bad)("outsider refused", f"status={status}")

    # 3. a figure question, with a figure citation
    status, body = call("POST", f"{API}/v1/query", member,
                        {"query": FIGURE_QUESTION, "tenant_id": TENANT, "top_k": 6, "stream": False})
    if status != 200 or not isinstance(body, dict):
        bad("figure citation", f"status={status} body={str(body)[:160]}")
    else:
        kinds = sorted({c.get("kind", "text") for c in body.get("citations") or []})
        fig = next((c for c in body.get("citations") or [] if c.get("kind") == "figure" and c.get("media_url")), None)
        if body.get("answerable") and fig:
            ok("figure citation", f"kinds={kinds} media_url={fig['media_url'][:60]}  {(body.get('answer') or '')[:60]!r}")
        else:
            bad("figure citation", f"answerable={body.get('answerable')} kinds={kinds} - is the media ingested? "
                                   f"make media && make ingest-corpus, then watch documind-ingest's log")

    # 4. the media documents, through the MCP server
    have_mcp = False
    if MCP_URL:
        try:
            docs = asyncio.run(mcp_list(token_as(IMPERSONATE, MCP_URL)))
            have_mcp = True
            media = [d["file"] for d in docs if str(d.get("file", "")).lower().endswith((".png", ".jpg", ".jpeg", ".mp4", ".mp3"))]
            (ok if media else bad)("media documents", f"{len(media)} of {len(docs)} indexed documents are media: {media[:6]}")
        except ImportError:
            print("  (fastmcp not installed: pip install --user fastmcp==3.4.7 - MCP legs skipped)")
        except Exception as e:  # noqa: BLE001
            bad("media documents", f"{type(e).__name__}: {str(e)[:160]}")

    # 5. the upload door: a signed PUT into the uploads bucket, then the worker
    q = f"filename={PROBE_NAME}&content_type=image/png&tenant_id={TENANT}"
    status, body = call("POST", f"{API}/v1/media/upload-url?{q}", member)
    if status != 200 or not isinstance(body, dict) or not body.get("url"):
        bad("upload-url", f"status={status} body={str(body)[:200]}")
    else:
        put_status, put_body = call("PUT", body["url"], None, raw=PROBE_PNG, content_type="image/png", timeout=60)
        if put_status not in (200, 201):
            bad("signed PUT", f"status={put_status} {str(put_body)[:160]}")
        else:
            ok("signed PUT", f"gs://{body.get('bucket')}/{body.get('blob')}")
            if have_mcp:
                deadline, seen = time.time() + 150, None
                while time.time() < deadline:
                    docs = asyncio.run(mcp_list(token_as(IMPERSONATE, MCP_URL), "all"))
                    seen = next((d for d in docs if d.get("file") == PROBE_NAME), None)
                    if seen and seen.get("status") in ("indexed", "failed"):
                        break
                    time.sleep(10)
                if seen and seen.get("status") == "indexed":
                    ok("worker indexed the PUT", f"chunks={seen.get('chunks')} at {seen.get('at')}")
                else:
                    bad("worker indexed the PUT", f"status={seen.get('status') if seen else 'never claimed'} "
                                                   f"error={seen.get('error') if seen else '-'} - read documind-ingest's log")
            else:
                print("  (no MCP client: the PUT succeeded; whether the worker indexed it is in documind-ingest's log)")

    print("  " + "-" * 56 + f"\n  {len(passed)} passed, {len(failed)} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
