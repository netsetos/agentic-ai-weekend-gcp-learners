#!/usr/bin/env bash
# make reindex FILE= NAME= TENANT= (mk/lifecycle.mk): re-issue ONE document under an object name (NAME= overrides the
# basename: the shape-A demo re-uploads evals/demo/hr_policy_2026_v2.md AS hr_policy_2026.md) and wait for the
# worker's line - ingest_ok with the retired count, or ingest_reactivated when the bytes are a version the ledger had
# retired (the undo). The golden set is checked first: a reindex is a release, and the rows move with the document.
# The same without make:  PROJECT=<id> TENANT=acme FILE=evals/demo/hr_policy_2026_v2.md NAME=hr_policy_2026.md bash commands/reindex.sh
set -uo pipefail
: "${PROJECT:?set PROJECT=<project id>}"
TENANT="${TENANT:-acme}"; PY="${PY:-python}"
[ -n "${FILE:-}" ] || { echo "FILE=<path> is required (TENANT=$TENANT, NAME=<object name, default the basename>)"; exit 2; }
OBJ="${NAME:-$(basename "$FILE")}"
"$PY" evals/run_eval.py --source "$OBJ" >/dev/null || { echo ">> the golden set is not sound: a reindex is a release, and the rows move with the document in the same commit - python evals/run_eval.py"; exit 1; }
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gcloud storage cp "$FILE" "gs://$PROJECT-uploads/$TENANT/$OBJ" && echo ">> gs://$PROJECT-uploads/$TENANT/$OBJ - waiting for the worker (up to 5 min)"
for i in $(seq 1 30); do sleep 10
  LINE=$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND (jsonPayload.event=\"ingest_ok\" OR jsonPayload.event=\"ingest_reactivated\") AND jsonPayload.tenant=\"$TENANT\" AND timestamp>=\"$SINCE\"" --project "$PROJECT" --limit 1 --format='value(jsonPayload.event,jsonPayload.doc_key,jsonPayload.chunks,jsonPayload.reused,jsonPayload.embedded,jsonPayload.retired,jsonPayload.effective_from)')
  if [ -n "$LINE" ]; then
    echo ">> event doc_key chunks reused embedded retired effective_from"; echo ">> $LINE"
    gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_superseded\" AND jsonPayload.tenant=\"$TENANT\" AND timestamp>=\"$SINCE\"" --project "$PROJECT" --limit 1 --format='value(jsonPayload.retired_doc_keys,jsonPayload.retired_chunks,jsonPayload.expire_days)' | sed 's/^/>> retired (doc_keys, chunks, expire days): /'
    echo ">> the gate, scoped to this document, on a candidate: make eval-live PROJECT=$PROJECT SOURCE=$OBJ API=<candidate url>"
    exit 0
  fi
done
echo ">> no ingest line in 5 min - gcloud logging read ... service_name=documind-ingest"; exit 1
