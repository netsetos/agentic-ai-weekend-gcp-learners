#!/usr/bin/env bash
# Push the corpus to the uploads bucket, one prefix per tenant.
#
# The prefix IS the tenancy boundary as far as ingestion is concerned: Eventarc fires on
# object.finalized, and services/ingest reads the tenant from the object path. Put a file
# under the wrong prefix and it is ingested as the wrong tenant - which is precisely what
# golden.jsonl's isolation rows are there to catch.
#
# Two kinds of file live under corpus/<tenant>/. The synthetic documents are .md and go up
# as they are (the worker decodes text/* itself). The real Acts (real_sources.json, fetched
# by fetch_real.py) are .pdf and go up as PDFs, so the worker parses them with Document AI
# the way a tenant's own upload would be parsed; the .md beside each PDF is the pypdf mirror
# for the offline gate and the notebooks, and uploading it too would index every real
# document twice. So: a .md whose .pdf twin exists stays home.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-documind-ai-YOUR-ID}"
BUCKET="gs://${PROJECT_ID}-uploads"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORPUS="${HERE}/corpus"

if [[ ! -d "${CORPUS}" ]]; then
  echo "corpus/ missing - run: python ${HERE}/build_corpus.py" >&2
  exit 1
fi

echo "project : ${PROJECT_ID}"
echo "bucket  : ${BUCKET}"
echo

for dir in "${CORPUS}"/*/; do
  tenant="$(basename "${dir}")"
  files=()
  for f in "${dir}"*; do
    [[ -f "${f}" ]] || continue
    if [[ "${f}" == *.md && ( -f "${f%.md}.pdf" || -f "${f%.md}.mp4" ) ]]; then
      continue                      # the mirror of a real PDF, or the script of the town hall video: offline use only
    fi
    if [[ "${f}" == *.segments.json ]]; then
      continue                      # make media --video's ground truth for 9.4's diarisation cell, not a document
    fi
    files+=("${f}")
  done
  echo "==> ${tenant} (${#files[@]} files) -> ${BUCKET}/${tenant}/"
  for f in "${files[@]}"; do
    echo "    $(basename "${f}")"
  done
  gcloud storage cp "${files[@]}" "${BUCKET}/${tenant}/"
done

echo
echo "Uploaded. Eventarc should now be enqueuing one ingest job per object;"
echo "watch the DLQ if nothing lands:"
echo "  gcloud pubsub subscriptions pull ingest-dlq-sub --auto-ack --limit=10"
echo
echo "The two binary assets are NOT here - manifest.json lists what to supply:"
grep -A1 '"supplied_by": "owner"' "${HERE}/manifest.json" | grep '"note"' \
  | sed 's/.*"note": "/  - /; s/"$//' || true
