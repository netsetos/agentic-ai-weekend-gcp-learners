#!/usr/bin/env bash
# Run or source this checkpoint. Its subshell leaves the operator's environment intact.
(
  set -euo pipefail
  : "${PROJECT:?Restore the intended project ID}"
  : "${DEMO_ROOT:?Restore the existing deployment directory}"
  cd "$DEMO_ROOT" || exit 1
  if ! command -v terraform >/dev/null 2>&1; then
    printf '%s\n' 'STOP: Terraform is not available on PATH. Restore the runbook shell/installed Terraform, then rerun this checkpoint; no query was attempted.' >&2
    exit 1
  fi

  # `export NAME=$(terraform ...)` masks Terraform's exit status. Read and check
  # every value first; publish query settings only after all four reads succeed.
  declare -A rag_tf_outputs=()
  for rag_tf_name in vector_index_endpoint vector_deployed_index_id embedding_model embedding_version; do
    if ! rag_tf_value="$(terraform -chdir=terraform output -raw "$rag_tf_name")"; then
      printf 'STOP: Terraform output %s failed. Check the existing state/backend and authentication; no query was attempted.\n' "$rag_tf_name" >&2
      exit 1
    fi
    if [[ -z "${rag_tf_value//[[:space:]]/}" ]]; then
      printf 'STOP: Terraform output %s is empty. Finish or inspect the existing infrastructure; no query was attempted.\n' "$rag_tf_name" >&2
      exit 1
    fi
    rag_tf_outputs["$rag_tf_name"]="$rag_tf_value"
  done

  export PROJECT GOOGLE_CLOUD_PROJECT="$PROJECT" PYTHONPATH="$DEMO_ROOT"
  export VECTOR_INDEX_ENDPOINT="${rag_tf_outputs[vector_index_endpoint]}"
  export VECTOR_DEPLOYED_INDEX_ID="${rag_tf_outputs[vector_deployed_index_id]}"
  export EMBEDDING_MODEL="${rag_tf_outputs[embedding_model]}"
  export EMBEDDING_VERSION="${rag_tf_outputs[embedding_version]}"
  if ! command -v python >/dev/null 2>&1; then
    printf '%s\n' 'STOP: Python is not available. Activate the runbook virtual environment and rerun this checkpoint.' >&2
    exit 1
  fi
  python - <<'PY'
import json, math, os
from pathlib import Path
from google.cloud import aiplatform, firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import HybridQuery, Namespace
from shared.sparse_encoder import sparse_encode, SPARSE_ENCODER_VERSION

project = os.environ['PROJECT']
db = firestore.Client(project=project)
uri = f'gs://{project}-uploads/acme/smoke_note.md'
rows = [snap for snap in db.collection('chunks').where(filter=FieldFilter('tenant_id','==','acme'))
        .where(filter=FieldFilter('source_uri','==',uri)).where(filter=FieldFilter('current','==',True)).stream()]
rows = [snap for snap in rows if 'lantern' in (snap.to_dict().get('text') or '').lower()]
assert rows, 'STOP: no current Acme smoke chunk mentioning lantern; ingest and verify the canonical smoke source first.'
seed_id = rows[0].id
seed = rows[0].to_dict()
missing = [key for key in ('embedding', 'embedding_model', 'embedding_version', 'text')
           if key not in seed or seed[key] is None]
assert not missing, f'STOP: smoke seed {seed_id} is missing required fields: {missing}. Inspect the current chunk before querying.'
try:
    dense = list(seed['embedding'])
except TypeError as error:
    raise AssertionError(f'STOP: smoke seed {seed_id} embedding is not a vector.') from error
assert len(dense) == 768, f'STOP: smoke seed {seed_id} has {len(dense)} embedding dimensions; this checkpoint requires 768.'
try:
    finite = all(math.isfinite(float(value)) for value in dense)
except (TypeError, ValueError) as error:
    raise AssertionError(f'STOP: smoke seed {seed_id} embedding contains nonnumeric values.') from error
assert finite, f'STOP: smoke seed {seed_id} embedding contains nonfinite values.'
expected_model = os.environ['EMBEDDING_MODEL']
expected_version = os.environ['EMBEDDING_VERSION']
assert seed['embedding_model'] == expected_model, (
    f'STOP: smoke seed {seed_id} embedding model is {seed["embedding_model"]!r}; Terraform expects {expected_model!r}.')
assert str(seed['embedding_version']) == expected_version, (
    f'STOP: smoke seed {seed_id} embedding version is {seed["embedding_version"]!r}; Terraform expects {expected_version!r}.')
values, dimensions = sparse_encode(seed['text'])
assert values and len(values) == len(dimensions) == len(set(dimensions)), 'STOP: sparse encoder returned empty or inconsistent values/dimensions.'
endpoint = aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name=os.environ['VECTOR_INDEX_ENDPOINT'])
filters = [Namespace(name='tenant_id',allow_tokens=['acme']), Namespace(name='current',allow_tokens=['true'])]
queries = {
    'dense': dense,
    'sparse_only': HybridQuery(sparse_embedding_values=values, sparse_embedding_dimensions=dimensions),
    'hybrid': HybridQuery(dense_embedding=dense, sparse_embedding_values=values,
                         sparse_embedding_dimensions=dimensions, rrf_ranking_alpha=0.5),
}
report = {'scope':'retrieval plumbing using a known current chunk, not a quality benchmark',
          'sparse_encoder':SPARSE_ENCODER_VERSION, 'seed_id':seed_id, 'results':{}}
for name, query in queries.items():
    found = endpoint.find_neighbors(deployed_index_id=os.environ['VECTOR_DEPLOYED_INDEX_ID'],
                                    queries=[query], num_neighbors=20, filter=filters)
    hits = found[0] if found else []
    assert hits, f'{name} returned no datapoints; check upsert/backfill and serving readiness'
    sources=[]
    for hit in hits:
        row = db.collection('chunks').document(hit.id).get().to_dict() or {}
        assert row.get('tenant_id')=='acme' and row.get('current') is True, (name,hit.id)
        sources.append(row.get('source_uri'))
    assert uri in sources, f'{name} did not return the control source'
    report['results'][name]={'ids':[hit.id for hit in hits], 'control_source_found':True}
Path('operator-evidence').mkdir(exist_ok=True)
Path('operator-evidence/hybrid-plumbing.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
print('PASS: dense, sparse-only and fused queries served current tenant-scoped evidence.')
PY
)
