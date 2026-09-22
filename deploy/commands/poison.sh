#!/usr/bin/env bash
# make poison TENANT= (mk/ingestion.mk): a zero-byte object in, then the worker's 400 and its ingest_poison line. The
# push subscription (eventarc.tf) retries a non-2xx with backoff, twelve attempts, then documind-ingest-dlq: make dlq.
# The same without make:  PROJECT=<id> TENANT=acme bash commands/poison.sh
set -uo pipefail
: "${PROJECT:?set PROJECT=<project id>}"
TENANT="${TENANT:-acme}"
SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ); NAME=poison-$(date +%s).pdf; : > "/tmp/$NAME"
gcloud storage cp "/tmp/$NAME" "gs://$PROJECT-uploads/$TENANT/$NAME" && echo ">> zero-byte object in: $NAME"
for i in $(seq 1 12); do sleep 10
  LINE=$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_poison\" AND timestamp>=\"$SINCE\"" --project "$PROJECT" --limit 1 --format='value(jsonPayload.error)')
  if [ -n "$LINE" ]; then
    echo ">> the worker refused it (400): $LINE"
    echo ">> Pub/Sub retries a non-2xx with backoff (10 s to 600 s, twelve attempts: eventarc.tf), then ingest-dlq: make dlq about an hour after this line"
    exit 0
  fi
done
echo ">> no ingest_poison line in 2 min"; exit 1
