#!/usr/bin/env bash
# make ingest-one FILE= TENANT= (mk/ingestion.mk): ONE object under the tenant's prefix, then the worker's ingest_ok line.
# The same without make:  PROJECT=<id> TENANT=acme FILE=evals/demo/smoke_note_v1.md bash commands/ingest-one.sh
set -uo pipefail
: "${PROJECT:?set PROJECT=<project id>}"
TENANT="${TENANT:-acme}"
[ -n "${FILE:-}" ] || { echo "FILE=<path> is required (TENANT=$TENANT)"; exit 2; }
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ); NAME=$(basename "$FILE")
gcloud storage cp "$FILE" "gs://$PROJECT-uploads/$TENANT/$NAME" && echo ">> gs://$PROJECT-uploads/$TENANT/$NAME - waiting for the worker (up to 5 min)"
for i in $(seq 1 30); do sleep 10
  LINE=$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_ok\" AND jsonPayload.tenant=\"$TENANT\" AND timestamp>=\"$SINCE\"" --project "$PROJECT" --limit 1 --format='value(jsonPayload.doc_key,jsonPayload.chunks,jsonPayload.pages)')
  if [ -n "$LINE" ]; then echo ">> indexed: $LINE"; exit 0; fi
done
echo ">> no ingest_ok in 5 min - gcloud logging read ... service_name=documind-ingest"; exit 1
