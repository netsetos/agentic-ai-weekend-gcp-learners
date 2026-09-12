#!/usr/bin/env python3
"""
DocuMind AI - live smoke test of the document lifecycle (12 September 2026).

The reindex, smoked like every other surface (make smoke-all). A small fixture goes into
the tenant's prefix twice - version 1, then version 2 under the SAME object name - and the
worker's own log lines prove the ledger: revision 2 indexed with the previous version's
unchanged chunks REUSED by hash and the changed ones embedded, revision 1 RETIRED (never
deleted), the answer moved; then version 1 again, REACTIVATED without a re-embedding, the
answer back. Uses only the standard library and the gcloud CLI (the upload, the log read,
the identity token), like smoke.py.

    export DOCUMIND_PROJECT=documind-ai-YOUR-ID
    export DOCUMIND_API_URL=https://documind-api-NUMBER.us-central1.run.app
    export DOCUMIND_TENANT=acme
    python deploy/smoke/smoke_reindex.py

Checks:
    1. v1 in (ingest_ok, or a reactivation / duplicate when a previous run left it there)
    2. v2 in, same name       -> ingest_ok with retired > 0, reused >= 1, embedded >= 1, the declared date
    3. ask                    -> the new value (bay 7), never the old one
    4. v1 in again            -> ingest_reactivated: nothing embedded, the old rows current again
    5. ask                    -> the old value (bay 4)
    6. GET /v1/sources        -> the ledger row for the fixture (tolerates 404 on an API before the view)

The fixture stays in the bucket at version 1 afterwards, one more indexed document of the
tenant; the nightly reconcile sees a source that matches its ledger row. Exit code is
non-zero if any required check fails.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.join(os.path.dirname(HERE), "evals", "demo")
API = os.environ.get("DOCUMIND_API_URL", "").rstrip("/")
PROJECT = os.environ.get("DOCUMIND_PROJECT", "")
TENANT = os.environ.get("DOCUMIND_TENANT", "acme")
IMPERSONATE = os.environ.get("DOCUMIND_IMPERSONATE_SA",
                             f"documind-ui-sa@{PROJECT}.iam.gserviceaccount.com" if PROJECT else "")
BUCKET = os.environ.get("DOCUMIND_UPLOAD_BUCKET", f"{PROJECT}-uploads" if PROJECT else "")
NAME = os.environ.get("DOCUMIND_SMOKE_OBJECT", "smoke_note.md")
QUESTION = "In which bay of the Pune warehouse is the smoke lantern kept?"
V1, V2 = os.path.join(DEMO, "smoke_note_v1.md"), os.path.join(DEMO, "smoke_note_v2.md")
WAIT_S = int(os.environ.get("DOCUMIND_SMOKE_WAIT_S", "300"))

passed, failed = [], []


def ok(name, detail=""):
    passed.append(name)
    print(f"  [PASS] {name}  {detail}")


def bad(name, detail=""):
    failed.append(name)
    print(f"  [FAIL] {name}  {detail}")


def gcloud(*args: str, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def identity_token() -> str | None:
    if not IMPERSONATE or not API:
        return None
    rc, out = gcloud("auth", "print-identity-token", "--include-email",
                     f"--impersonate-service-account={IMPERSONATE}", f"--audiences={API}")
    if rc != 0:
        print(f"  (could not mint identity token via gcloud: {out.strip()[:200]})")
        return None
    return out.strip().splitlines()[-1]


def upload(path: str) -> str:
    uri = f"gs://{BUCKET}/{TENANT}/{NAME}"
    rc, out = gcloud("storage", "cp", path, uri, "--project", PROJECT)
    if rc != 0:
        raise RuntimeError(f"upload failed: {out.strip()[:300]}")
    return uri


def wait_for(events: tuple[str, ...], since: dt.datetime, seconds: int = WAIT_S) -> dict | None:
    """The worker's line for this upload: polls Cloud Logging every 10 s, like make reindex."""
    ev = " OR ".join(f'jsonPayload.event="{e}"' for e in events)
    q = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="documind-ingest" AND ({ev}) '
         f'AND timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}" AND jsonPayload.tenant="{TENANT}"')
    deadline = time.time() + seconds
    while time.time() < deadline:
        rc, out = gcloud("logging", "read", q, "--project", PROJECT, "--limit", "1", "--format=json")
        try:
            rows = json.loads(out) if rc == 0 else []
        except json.JSONDecodeError:
            rows = []
        if rows:
            return rows[0].get("jsonPayload") or {}
        time.sleep(10)
    return None


def ask(token: str | None) -> tuple[int | None, dict]:
    if not API:
        return None, {}
    req = urllib.request.Request(API + "/v1/query", method="POST",
                                 data=json.dumps({"query": QUESTION, "tenant_id": TENANT, "user_id": "u_smoke",
                                                  "top_k": 5, "stream": False}).encode())
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}
    except Exception as e:
        return None, {"error": str(e)}


def answer_says(body: dict, phrase: str) -> bool:
    return phrase in (body.get("answer") or "").lower().replace("-", " ")


def main() -> int:
    if not PROJECT:
        print("DOCUMIND_PROJECT is not set - this is a LIVE test of the ingest worker's ledger.")
        print(__doc__)
        return 2
    if not (os.path.isfile(V1) and os.path.isfile(V2)):
        print(f"fixtures missing under {DEMO}: smoke_note_v1.md / smoke_note_v2.md")
        return 2
    print(f"\n  DocuMind AI - reindex smoke\n  bucket: gs://{BUCKET}/{TENANT}/{NAME}  api: {API or '(none: the asks are skipped)'}\n  " + "-" * 56)
    token = identity_token()

    # 1. version 1 in - a fresh index, or the leftover of the last run (a duplicate, or a reactivation)
    since = dt.datetime.now(dt.timezone.utc)
    upload(V1)
    line = wait_for(("ingest_ok", "ingest_reactivated", "ingest_duplicate"), since)
    if line and line.get("event") in ("ingest_ok", "ingest_reactivated", "ingest_duplicate"):
        ok("v1 indexed", f"{line.get('event')} chunks={line.get('chunks', '-')}")
    else:
        bad("v1 indexed", "no worker line in time - is documind-ingest deployed and the push subscription pointing at it?")
        return 1

    # 2. version 2 in, the SAME object name: a re-issue, not a new document
    since = dt.datetime.now(dt.timezone.utc)
    upload(V2)
    line = wait_for(("ingest_ok", "ingest_failed"), since)
    if not line or line.get("event") != "ingest_ok":
        bad("v2 reindexed", f"got {line.get('event') if line else 'nothing'}: {str(line)[:200]}")
        upload(V1)
        return 1
    retired, reused, embedded = line.get("retired"), line.get("reused"), line.get("embedded")
    if retired and retired > 0 and reused is not None and embedded is not None and reused >= 1 and embedded >= 1:
        ok("v2 reindexed", f"chunks={line.get('chunks')} reused={reused} embedded={embedded} retired={retired} "
                           f"effective_from={line.get('effective_from')}")
    elif retired and retired > 0 and reused is None:
        bad("v2 reindexed", f"retired={retired} but no reused/embedded counts: the worker on the lane predates the carry-over")
    else:
        bad("v2 reindexed", f"retired={retired} reused={reused} embedded={embedded} - the previous version was not retired, "
                            "or nothing was carried over")
    if line.get("effective_from") != "2026-10-01":
        bad("v2 dated", f"effective_from={line.get('effective_from')!r}, expected 2026-10-01 from the document's own line")
    else:
        ok("v2 dated", "effective_from=2026-10-01 read off the document")

    # 3. the answer moved - and never mentions the retired figure
    if API:
        time.sleep(8)
        st, body = ask(token)
        if st == 200 and answer_says(body, "bay 7") and not answer_says(body, "bay 4"):
            ok("answer moved", (body.get("answer") or "")[:90].replace(chr(10), " "))
        else:
            bad("answer moved", f"status={st} answer={(body.get('answer') or body.get('error') or '')[:120]!r}")

    # 4. version 1 again: the undo - reactivated, nothing embedded
    since = dt.datetime.now(dt.timezone.utc)
    upload(V1)
    line = wait_for(("ingest_reactivated", "ingest_ok", "ingest_failed"), since)
    if line and line.get("event") == "ingest_reactivated":
        ok("v1 reactivated", f"chunks={line.get('chunks')} embedded={line.get('embedded', 0)} retired={line.get('retired')}")
    elif line and line.get("event") == "ingest_ok":
        bad("v1 reactivated", "the worker re-indexed v1 instead of reactivating its retired rows (were they purged, or never retired?)")
    else:
        bad("v1 reactivated", f"got {line.get('event') if line else 'nothing'}")

    # 5. the answer is back
    if API:
        time.sleep(8)
        st, body = ask(token)
        if st == 200 and answer_says(body, "bay 4") and not answer_says(body, "bay 7"):
            ok("answer restored", (body.get("answer") or "")[:90].replace(chr(10), " "))
        else:
            bad("answer restored", f"status={st} answer={(body.get('answer') or body.get('error') or '')[:120]!r}")

        # 6. the versions view
        req = urllib.request.Request(f"{API}/v1/sources?tenant_id={TENANT}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                led = json.loads(r.read().decode())
            row = next((s for s in led.get("sources", []) if (s.get("name") or "").endswith("/" + NAME)), None)
            if row and row.get("status") == "indexed":
                ok("ledger row", f"{row['name']} reused={row.get('reused')} embedded={row.get('embedded')} "
                                 f"retired={row.get('retired')} fingerprint={led.get('fingerprint')}")
            else:
                bad("ledger row", f"no indexed row for {NAME} in /v1/sources ({len(led.get('sources', []))} rows)")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("  [ -- ] ledger row  this API revision has no /v1/sources yet - skipped")
            else:
                bad("ledger row", f"HTTP {e.code}")
        except Exception as e:
            bad("ledger row", str(e)[:120])

    print("  " + "-" * 56)
    print(f"  {len(passed)} pass · {len(failed)} fail\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
