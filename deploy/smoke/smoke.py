#!/usr/bin/env python3
"""
DocuMind AI — live smoke test (Tier B).

Run this AFTER `make up` against a real deployment to prove the RAG API is
alive end-to-end. Uses only the Python standard library + the `gcloud` CLI
(for the identity token), so it runs anywhere gcloud is authenticated.

It is NOT part of the offline dry run — offline, validate.py only syntax-checks
this file. On live day:

    export DOCUMIND_API_URL=https://documind-api-xxx.run.app
    export DOCUMIND_PROJECT=documind-ai-live-0901
    python deploy/smoke/smoke.py

Checks:
    1. GET  /health                -> 200 {"status":"ok"}
    2. GET  /ready                 -> 200 (tolerates 404 if not implemented)
    3. POST /v1/query              -> 200 with answer + citations + answerable
    3b. the same question with no token -> 401/403 (the door refuses before the roster is asked)
    3c. (SEMANTIC_CACHE=on only) the same question again -> a hit: cache_hit=semantic, backend=cache,
        the same citations. Off, the line says so and asserts nothing.
    4. (optional) BigQuery query-log row for today (informational)
    5. (optional) documind-chat remembers across two requests - 8.5's gate, the checkpointer.
       Needs DOCUMIND_CHAT_URL and DOCUMIND_CHAT_TOKEN (an ID token IAP accepts for the chat
       surface, e.g. `gcloud auth print-identity-token --audiences=<IAP client id>`).

Identity: the ID token IS who you are (12.8, shared/iap.py's bearer leg). rag-api verifies it
and checks the impersonated account against the tenant roster - so DOCUMIND_IMPERSONATE_SA
must be a member of DOCUMIND_TENANT, or check 3 is a 403. No x-user-email / x-tenant-id
headers are sent: nothing reads them outside AUTH_MODE=dev, and this is a live test.

Exit code is non-zero if any required check fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = os.environ.get("DOCUMIND_API_URL", "").rstrip("/")
PROJECT = os.environ.get("DOCUMIND_PROJECT", "")
IMPERSONATE = os.environ.get(
    "DOCUMIND_IMPERSONATE_SA",
    f"documind-ui-sa@{PROJECT}.iam.gserviceaccount.com" if PROJECT else "",
)
BQ_DATASET = os.environ.get("DOCUMIND_BQ_DATASET", "documind_observability")
TENANT = os.environ.get("DOCUMIND_TENANT", "tenant-smoke")
CHAT = os.environ.get("DOCUMIND_CHAT_URL", "").rstrip("/")
CHAT_TOKEN = os.environ.get("DOCUMIND_CHAT_TOKEN", "")

passed, failed = [], []


def ok(name, detail=""):
    passed.append(name)
    print(f"  [PASS] {name}  {detail}")


def bad(name, detail=""):
    failed.append(name)
    print(f"  [FAIL] {name}  {detail}")


def identity_token() -> str | None:
    if not IMPERSONATE:
        return None
    try:
        # --audiences is not optional: Cloud Run checks the token was minted FOR its URL, and
        # gcloud refuses to impersonate without one. --include-email is not optional either:
        # without it the token names no email, and the API - rightly - refuses a caller it
        # cannot put on a roster. Both were missing until the first live run.
        out = subprocess.run(
            ["gcloud", "auth", "print-identity-token", "--include-email",
             f"--impersonate-service-account={IMPERSONATE}", f"--audiences={API}"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"  (could not mint identity token via gcloud: {(e.stderr or str(e)).strip()[:300]})")
        return None
    except Exception as e:
        print(f"  (could not mint identity token via gcloud: {e})")
        return None


def call(method, path, token, body=None, headers=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")   # the identity - nothing else is read
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return None, str(e)


def main() -> int:
    if not API:
        print("DOCUMIND_API_URL is not set — this is a LIVE test, run it after `make up`.")
        print(__doc__)
        return 2
    print(f"\n  DocuMind AI — live smoke test\n  target: {API}\n  " + "-" * 56)

    token = identity_token()
    if IMPERSONATE and not token:
        print("  (proceeding unauthenticated — expect 401/403 on a private service)")

    # 1. health
    st, body = call("GET", "/health", token)
    if st == 200 and '"ok"' in body:
        ok("health", body[:60])
    else:
        bad("health", f"status={st} body={body[:80]}")

    # 2. ready (optional)
    st, body = call("GET", "/ready", token)
    if st == 200:
        ok("ready", body[:60])
    elif st == 404:
        print("  [ -- ] ready  not implemented (404) — skipped")
    else:
        bad("ready", f"status={st} body={body[:80]}")

    # 3. query - one the corpus ANSWERS. The first version asked "Which file types are
    # supported?", a question from a demo corpus that no longer exists; the API refused it,
    # correctly, and the test counted the refusal as a pass. A smoke test that passes on a
    # refusal proves nothing about retrieval. This question is golden.jsonl's lk-16: the
    # Payment of Gratuity Act, 1972, in every tenant's corpus, and the answer must cite it.
    q = {"query": os.environ.get("DOCUMIND_SMOKE_QUESTION",
                                 "After how many years of continuous service does gratuity become payable?"),
         "tenant_id": TENANT, "user_id": "u_smoke", "top_k": 5, "stream": False}
    j = {}
    st, body = call("POST", "/v1/query", token, body=q)
    if st == 200:
        try:
            j = json.loads(body)
            have = all(k in j for k in ("answer", "citations", "answerable"))
            cites = len(j.get("citations", []))
            if not have:
                bad("query", f"missing keys in response: {list(j)[:6]}")
            elif j.get("answerable") and cites:
                ok("query", f"answerable=True citations={cites}  {(j.get('answer') or '')[:70]!r}")
            else:
                bad("query", f"refused or uncited: answerable={j.get('answerable')} citations={cites} - "
                             "is the corpus ingested for this tenant (documents/ status indexed)?")
        except json.JSONDecodeError:
            bad("query", f"non-JSON 200: {body[:80]}")
    else:
        bad("query", f"status={st} body={body[:120]}")

    # 3b. the same question with NO token: the door must refuse before the roster is even asked. 12.8: a smoke
    # made only of "does it work?" cannot tell an open service from a closed one, so this line asserts a refusal.
    st, body = call("POST", "/v1/query", None, body=q)
    if st in (401, 403):
        ok("no token refused", f"status={st}")
    else:
        bad("no token refused", f"status={st} - the service answered an anonymous caller")

    # 3c. the answer cache (12.6, SEMANTIC_CACHE=on): the same question again is a hit - answered from Firestore with
    # the citations it had, backend=cache, cost 0 - and identical citations prove it is the SAME answer, not a near
    # one. Off (the lane's default), the line says so and asserts nothing: a cache that is off is not a failure.
    st, vbody = call("GET", "/version", token)
    try:
        cache_on = st == 200 and json.loads(vbody).get("semantic_cache") == "on"
    except json.JSONDecodeError:
        cache_on = False
    if cache_on:
        st, body = call("POST", "/v1/query", token, body=q)
        try:
            j2 = json.loads(body) if st == 200 else {}
        except json.JSONDecodeError:
            j2 = {}
        same = [c.get("chunk_id") for c in j2.get("citations", [])] == [c.get("chunk_id") for c in j.get("citations", [])]
        if j2.get("cache_hit") == "semantic" and j2.get("backend") == "cache" and same and j2.get("citations"):
            ok("semantic cache", f"hit in {j2.get('latency_ms')} ms, backend=cache, the same {len(j2['citations'])} citations")
        else:
            bad("semantic cache", f"second ask was not a hit: status={st} cache_hit={j2.get('cache_hit')} "
                                  f"backend={j2.get('backend')} same_citations={same}")
    else:
        print("  [ -- ] semantic cache  SEMANTIC_CACHE is off on this service (/version) — skipped")

    # 4. observability row (optional, informational)
    if PROJECT:
        try:
            # The sink writes to run_googleapis_com_stdout — Cloud Logging names the table
            # after the LOG, not after anything we choose. This check used to query
            # `query_logs`, a table that never existed, so it printed "not queryable yet"
            # forever and a genuinely broken sink looked exactly the same as a lagging one.
            out = subprocess.run(
                ["bq", "--project_id", PROJECT, "query", "--nouse_legacy_sql",
                 "--format=csv",
                 f"SELECT COUNT(*) FROM `{PROJECT}.{BQ_DATASET}.run_googleapis_com_stdout` "
                 f"WHERE DATE(timestamp)=CURRENT_DATE()"],
                capture_output=True, text=True, timeout=40,
            )
            if out.returncode == 0:
                print(f"  [info] sink rows today: {out.stdout.strip().splitlines()[-1]}")
            else:
                print("  [info] sink table not queryable yet (sink may lag) — non-fatal")

            # And the thing 12.3's gate actually asks for: tenant_daily non-empty.
            out = subprocess.run(
                ["bq", "--project_id", PROJECT, "query", "--nouse_legacy_sql",
                 "--format=csv",
                 f"SELECT COUNT(*) FROM `{PROJECT}.{BQ_DATASET}.tenant_daily`"],
                capture_output=True, text=True, timeout=40,
            )
            if out.returncode == 0:
                rows = out.stdout.strip().splitlines()[-1]
                print(f"  [info] tenant_daily rows: {rows}"
                      + ("" if rows not in ("0", "") else
                         "  <-- EMPTY. Ask five questions through the UI, then re-run. "
                         "If it stays empty, check the sink filter matches BOTH "
                         "event=query and event=stream."))
            else:
                print("  [info] tenant_daily not queryable yet — non-fatal")
        except Exception:
            print("  [info] skipped BigQuery check (bq CLI unavailable)")

    # 5. the conversation survives a restart (8.5's gate, gap G5). Two requests, one session id:
    #    on a scale-to-zero service they may land on different instances, so a recalled word
    #    proves the checkpointer is SHARED - which is the property, not a literal restart.
    if CHAT and CHAT_TOKEN:
        sid = f"smoke-{os.getpid()}"

        def chat_call(question):
            req = urllib.request.Request(CHAT + "/v1/chat", method="POST",
                                         data=json.dumps({"question": question, "session_id": sid}).encode())
            req.add_header("Authorization", f"Bearer {CHAT_TOKEN}")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
            except Exception as e:
                return None, str(e)

        st1, b1 = chat_call("Remember this word for me: tamarind. Just confirm you have it.")
        st2, b2 = chat_call("Which word did I ask you to remember?")
        if st1 == 200 and st2 == 200 and "tamarind" in b2.lower():
            ok("chat memory", f"session {sid} recalled the word on the second request")
        else:
            bad("chat memory", f"turn1={st1} turn2={st2} body2={b2[:100]}")
    elif CHAT:
        print("  [ -- ] chat memory  set DOCUMIND_CHAT_TOKEN (an ID token IAP accepts) to run it")

    print("  " + "-" * 56)
    print(f"  {len(passed)} pass · {len(failed)} fail\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
