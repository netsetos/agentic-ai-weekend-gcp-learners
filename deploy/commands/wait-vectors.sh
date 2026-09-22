#!/usr/bin/env bash
# make wait-vectors [WANT=n] (mk/ingestion.mk): block until the tier holds >= WANT datapoints (default 1), 20 minutes at
# most, or say why not. The index is VECTOR_INDEX_NAME when the shell has it (the worker's own variable), else
# Terraform's vector_index_name output. The same without make:  PROJECT=<id> REGION=asia-south1 WANT=3 bash commands/wait-vectors.sh
set -uo pipefail
: "${PROJECT:?set PROJECT=<project id>}"
REGION="${REGION:-us-central1}"; TF_DIR="${TF_DIR:-terraform}"; WANT="${WANT:-1}"; N=0
IDX="${VECTOR_INDEX_NAME:-}"
[ -n "$IDX" ] || IDX=$(cd "$TF_DIR" 2>/dev/null && terraform output -raw vector_index_name 2>/dev/null)
[ -n "$IDX" ] || { echo ">> no index name: set VECTOR_INDEX_NAME, or apply vector.tf (terraform output vector_index_name)"; exit 1; }
echo ">> waiting for >= $WANT datapoints in $(basename "$IDX")"
for i in $(seq 1 60); do
  N=$(gcloud ai indexes describe "$(basename "$IDX")" --region="$REGION" --project="$PROJECT" --format='value(indexStats.vectorsCount)' 2>/dev/null); N="${N:-0}"
  if [ "$N" -ge "$WANT" ]; then echo ">> $N datapoints"; exit 0; fi
  sleep 20
done
echo ">> still $N datapoints after 20 min. Either the worker never upserted (documind-ingest logs: ingest_ok,"
echo ">> and VECTOR_INDEX_NAME on the service), or the corpus never landed (make ingest-corpus)."
echo ">> make backfill-vectors APPLY=1 fills the tier from the rows Firestore already holds."; exit 1
