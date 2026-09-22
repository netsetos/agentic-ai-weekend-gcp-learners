#!/usr/bin/env bash
# make vector-status (mk/ingestion.mk): the index and its deployment as gcloud sees them - the datapoint count, the
# update method, the deployed index and its last sync. Names come from the shell (VECTOR_INDEX_NAME,
# VECTOR_INDEX_ENDPOINT: the worker's and the API's own variables) or from Terraform's outputs.
# The same without make:  PROJECT=<id> REGION=asia-south1 bash commands/vector-status.sh   (or: python commands/lane.py vector-status)
set -uo pipefail
: "${PROJECT:?set PROJECT=<project id>}"
REGION="${REGION:-us-central1}"; TF_DIR="${TF_DIR:-terraform}"
IDX="${VECTOR_INDEX_NAME:-}"; EP="${VECTOR_INDEX_ENDPOINT:-}"
[ -n "$IDX" ] || IDX=$(cd "$TF_DIR" 2>/dev/null && terraform output -raw vector_index_name 2>/dev/null)
[ -n "$IDX" ] || { echo ">> no vector_index_name in the state and no VECTOR_INDEX_NAME in the shell: vector.tf is not applied"; exit 0; }
gcloud ai indexes describe "$(basename "$IDX")" --region="$REGION" --project="$PROJECT" \
  --format='yaml(displayName,indexStats.vectorsCount,indexStats.shardsCount,indexUpdateMethod)'
[ -n "$EP" ] || EP=$(cd "$TF_DIR" 2>/dev/null && terraform output -raw vector_index_endpoint 2>/dev/null)
[ -n "$EP" ] || { echo ">> no endpoint name (VECTOR_INDEX_ENDPOINT or terraform output vector_index_endpoint): the deployment was not read"; exit 0; }
gcloud ai index-endpoints describe "$(basename "$EP")" --region="$REGION" --project="$PROJECT" \
  --format='yaml(displayName,deployedIndexes[].id,deployedIndexes[].indexSyncTime)'
