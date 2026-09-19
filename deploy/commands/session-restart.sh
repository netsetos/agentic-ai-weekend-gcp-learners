#!/usr/bin/env bash
# Source this file; it only defines functions. It never resumes or deploys automatically.
# RAG_SESSION_HOME is an optional root for isolated tests; ordinary use defaults to HOME.

_rag_session_keys() {
  printf '%s\n' PROJECT REGION OPERATOR_EMAIL PROJECT_NUMBER RESIDENCY DEMO_ROOT \
    SOURCE_BRANCH SOURCE_COMMIT GIT_SHA RAG_SOURCE_REPO RAG_SOURCE_BACKUP \
    TFSTATE_BUCKET BUILD_SOURCE_BUCKET BILLING_ACCOUNT_ID BILLING_ACCOUNT_NAME \
    TF_VAR_github_repository TF_VAR_github_repository_id TF_VAR_billing_account_id \
    TF_VAR_india_region TF_VAR_residency TF_VAR_env TF_VAR_audit_lock \
    TF_VAR_gemini_quota_override TF_VAR_budget_amount TF_VAR_budget_currency \
    TF_VAR_budget_pubsub TF_VAR_alert_channels TF_VAR_alert_emails \
    TF_VAR_enforce_no_sa_keys TF_VAR_deploy_ref TF_VAR_embedding_model \
    TF_VAR_embedding_version TF_VAR_retention_days TF_VAR_managed_search \
    TF_VAR_managed_mirror TF_VAR_tenants TF_VAR_reconcile_job TF_VAR_batch_job \
    TF_VAR_reconcile_image RAG_GRAPH_SAVED_PROJECT GRAPH_EVIDENCE_DIR SPANNER_INSTANCE SPANNER_DATABASE \
    GRAPH_SEED_K GRAPH_QUESTION GRAPH_SEED_DISTANCE EMBEDDING_MODEL EMBED_LOCATION
}

_rag_stop() { printf 'STOP: %s\n' "$*" >&2; return 1; }

_rag_add_cli_directory() {
  local directory="$1"
  [[ -d "$directory" ]] || return 0
  case ":${PATH:-}:" in
    *":$directory:"*) ;;
    *) export PATH="${PATH:+$PATH:}$directory" ;; # keep the activated Python venv first
  esac
  hash -r
}

_rag_check_terraform() {
  local severity="$1" root="${RAG_SESSION_HOME:-$HOME}" system_root="${RAG_SESSION_HOME:-}"
  local executable candidate version_json
  hash -r
  executable=$(type -P terraform || true)
  if [[ -z "$executable" ]]; then
    for candidate in "$root/.local/bin/terraform" "$system_root/usr/local/bin/terraform" "$system_root/usr/bin/terraform"; do
      if [[ -f "$candidate" && -x "$candidate" ]]; then
        _rag_add_cli_directory "${candidate%/*}"
        executable="$candidate"
        break
      fi
    done
  fi
  if [[ -z "$executable" ]]; then
    printf '%s: Terraform is not installed or is outside PATH. Complete the Terraform installation block in runbook Step 1, then rerun rag_require_terraform.\n' "$severity" >&2
    return 1
  fi
  if ! version_json=$("$executable" version -json); then
    printf '%s: Terraform at %s could not report its version. Check the executable using runbook Step 1 before a Terraform checkpoint.\n' "$severity" "$executable" >&2
    return 1
  fi
  python - "$executable" "$version_json" "$severity" <<'TERRAFORM'
import json, re, sys
path, payload, severity = sys.argv[1:]
try:
    version = json.loads(payload)['terraform_version']
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?', version)
    if not match or tuple(map(int, match.groups())) < (1, 9, 0):
        raise ValueError(f'unsupported version {version!r}; stable Terraform >= 1.9.0 is required')
except (ValueError, KeyError, TypeError) as error:
    raise SystemExit(f'{severity}: Terraform version check failed: {error}. Complete the Terraform installation block in runbook Step 1, then retry.')
print(f'PASS: Terraform {version} is available at {path}')
TERRAFORM
}

rag_require_terraform() {
  [[ $# == 0 ]] || { _rag_stop 'Usage: rag_require_terraform'; return 2; }
  _rag_check_terraform STOP
}

_rag_legacy_graph_exports() (
  local root="${RAG_SESSION_HOME:-$HOME}" mode="${1:-resume}" key intended_project="${PROJECT:-}" intended_number="${PROJECT_NUMBER:-}"
  local -a keys=(GRAPH_EVIDENCE_DIR SPANNER_INSTANCE SPANNER_DATABASE GRAPH_SEED_K GRAPH_QUESTION GRAPH_SEED_DISTANCE EMBEDDING_MODEL EMBED_LOCATION)
  local -A present=()
  for key in "${keys[@]}"; do [[ -z "${!key:-}" ]] || present[$key]=1; done
  source "$root/rag-graph-resume.env" || exit
  export PROJECT="$intended_project" PROJECT_NUMBER="$intended_number"
  export GRAPH_EVIDENCE_DIR RAG_GRAPH_SAVED_PROJECT
  if ! python - <<'LEGACY'
import json, os
from pathlib import Path
project, recorded = os.environ['PROJECT'], os.environ.get('RAG_GRAPH_SAVED_PROJECT')
valid = recorded == project if recorded else False
if not recorded:
    try:
        x = json.loads(Path(os.environ['GRAPH_EVIDENCE_DIR'], 'api-before.json').read_text())
        namespace = str(x.get('metadata', {}).get('namespace', ''))
        valid = bool(namespace) and namespace in {project, os.environ.get('PROJECT_NUMBER', '')}
    except (OSError, ValueError, KeyError):
        pass
raise SystemExit(0 if valid else 1)
LEGACY
  then
    if [[ "$mode" == save ]]; then
      printf 'SKIP: legacy graph pointer is not verified for this project; no graph settings imported.\n' >&2
      exit 0
    fi
    _rag_stop 'Legacy graph pointer belongs to another project or cannot be verified.'; exit 1
  fi
  for key in "${keys[@]}"; do
    if [[ ! ${present[$key]+x} && ${!key+x} ]]; then printf 'export %s=%q\n' "$key" "${!key}" || exit; fi
  done
)

_rag_verify_base_snapshot() (
  local root="${RAG_SESSION_HOME:-$HOME}" key
  local -A expected=()
  for key in DEMO_ROOT RAG_SOURCE_REPO SOURCE_BRANCH SOURCE_COMMIT GIT_SHA RAG_SOURCE_BACKUP; do
    expected[$key]="${!key-}"
  done
  test -s "$root/rag-source-session.env" || { _rag_stop 'Missing rag-source-session.env; restore the original source setup.'; exit 1; }
  source "$root/rag-source-session.env" || exit
  for key in DEMO_ROOT RAG_SOURCE_REPO SOURCE_BRANCH SOURCE_COMMIT GIT_SHA RAG_SOURCE_BACKUP; do
    test -n "${!key-}" || { _rag_stop "Original source session has no $key."; exit 1; }
    if [[ -n "${expected[$key]}" && "${expected[$key]}" != "${!key}" ]]; then
      _rag_stop "$key disagrees with the original source session. Inspect the saved snapshots; do not fetch a newer branch."; exit 1
    fi
  done
  [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ && "$GIT_SHA" == "$SOURCE_COMMIT" ]] || { _rag_stop 'Invalid original source commit/GIT_SHA.'; exit 1; }
  test -d "$DEMO_ROOT" && test -d "$RAG_SOURCE_REPO" || { _rag_stop 'Original deployment or source directory is missing.'; exit 1; }
  git -C "$RAG_SOURCE_REPO" cat-file -e "$SOURCE_COMMIT^{commit}"
)

_rag_graph_exports() {
  python - <<'PY'
import json, math, os, shlex
from pathlib import Path

def stop(message):
    raise SystemExit('STOP: ' + message)
def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        stop(f'Cannot read {path}: {exc}')

root = Path(os.environ['DEMO_ROOT'])
out = Path(os.environ['GRAPH_EVIDENCE_DIR'])
if not out.is_dir():
    stop('Saved graph evidence directory is missing; do not create a replacement directory.')
manifest = read(root / 'operator-evidence/graph-source.json')
if manifest.get('project') != os.environ['PROJECT']:
    stop('Graph source evidence belongs to another project.')
if not (out / 'api-before.json').is_file():
    stop('C1 has not saved api-before.json yet. Resume C1 in this same evidence directory; do not create a new directory.')
saved = read(out / 'api-before.json')
namespace = str(saved.get('metadata', {}).get('namespace', ''))
if namespace not in {os.environ['PROJECT'], os.environ.get('PROJECT_NUMBER', '')} or not namespace:
    stop('Original API backup belongs to another project or its namespace cannot be verified.')
pin = out / 'acme-pin-before.json'
if pin.exists() and 'retrieval_backend' not in read(pin):
    stop('Original Acme pin backup is invalid; preserve it for review.')
env = {v['name']: v.get('value', '') for v in saved.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [{}])[0].get('env', [])}
values = {'SPANNER_INSTANCE': os.environ.get('SPANNER_INSTANCE') or env.get('SPANNER_INSTANCE') or 'documind-graph',
          'SPANNER_DATABASE': os.environ.get('SPANNER_DATABASE') or env.get('SPANNER_DATABASE') or 'documind',
          'EMBEDDING_MODEL': os.environ.get('EMBEDDING_MODEL') or env.get('EMBEDDING_MODEL') or 'text-embedding-005',
          'EMBED_LOCATION': os.environ.get('EMBED_LOCATION') or env.get('REGION') or 'us-central1'}
saved_distance = os.environ.get('GRAPH_SEED_DISTANCE')
if saved_distance:
    try:
        numeric_distance = float(saved_distance)
    except ValueError:
        stop('Saved GRAPH_SEED_DISTANCE is not numeric.')
    if not math.isfinite(numeric_distance) or not 0 <= numeric_distance <= 2:
        stop('Saved GRAPH_SEED_DISTANCE must be finite and in [0, 2].')
k = os.environ.get('GRAPH_SEED_K')
if k and (not k.isdigit() or int(k) < 1):
    stop('Saved GRAPH_SEED_K must be a positive integer.')
ask = out / 'ask-spanner.json'
if ask.exists():
    x = read(ask)
    if x.get('backend') != 'spanner' or x.get('seeded_by') != 'meaning':
        stop('Saved C3 result is not Spanner meaning-based retrieval.')
    if not isinstance(x.get('question'), str) or not x['question'].strip():
        stop('Saved C3 question is missing.')
    if not x.get('seeds') or not x.get('chunk_ids') or not all(isinstance(c, str) and c.startswith('acme:') for c in x['chunk_ids']):
        stop('Saved C3 result has no accepted Acme seeds/evidence. Resume C3 before C4.')
    distance = x.get('seed_distance')
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or not math.isfinite(distance) or not 0 <= distance <= 2:
        stop('Saved C3 distance is not a finite cosine-distance threshold in [0, 2].')
    saved_question = os.environ.get('GRAPH_QUESTION')
    if saved_question and saved_question != x['question']:
        stop('Saved question differs from the old C3 result. Rerun C3 before C4; do not reuse stale evidence.')
    if saved_distance and float(saved_distance) != distance:
        stop('Saved distance differs from the old C3 result. Rerun C3 before C4; do not reuse stale evidence.')
    values.update(GRAPH_QUESTION=x['question'], GRAPH_SEED_DISTANCE=str(distance))
for key, value in values.items():
    print(f'export {key}={shlex.quote(value)}')
PY
}

_rag_verify_graph_snapshot() (
  set -euo pipefail
  local file="$DEMO_ROOT/operator-evidence/graph-source.env" expected_root="$DEMO_ROOT" expected_repo="$RAG_SOURCE_REPO"
  test -s "$file" || { _rag_stop 'Missing C0 graph-source.env; resume C0 before graph steps.'; exit 1; }
  source "$file" || exit
  test "$DEMO_ROOT" = "$expected_root" && test "$RAG_SOURCE_REPO" = "$expected_repo" || { _rag_stop 'Graph snapshot paths disagree with this deployment.'; exit 1; }
  python - <<'PY' || exit
import json, os
from pathlib import Path
x = json.loads(Path(os.environ['DEMO_ROOT'], 'operator-evidence/graph-source.json').read_text())
assert x.get('project') == os.environ['PROJECT'], 'STOP: graph source project differs.'
assert x.get('source_commit') == os.environ['SOURCE_COMMIT'], 'STOP: graph source commit records disagree.'
assert os.environ['GIT_SHA'] == os.environ['SOURCE_COMMIT'], 'STOP: graph snapshot GIT_SHA differs.'
PY
  git -C "$RAG_SOURCE_REPO" cat-file -e "$SOURCE_COMMIT^{commit}"
)

rag_save_session() (
  set -u
  local root="${RAG_SESSION_HOME:-$HOME}" key temporary assignments
  : "${PROJECT:?Set PROJECT before saving}" "${REGION:?Set REGION before saving}" "${DEMO_ROOT:?Set DEMO_ROOT before saving}"
  _rag_verify_base_snapshot || exit
  source "$root/rag-source-session.env" || exit
  # Saving progress must work before C1 backups/C3 requests have completed.
  if [[ -z "${GRAPH_EVIDENCE_DIR:-}" && -s "$root/rag-graph-resume.env" ]]; then
    assignments=$(_rag_legacy_graph_exports save) || exit
    eval "$assignments" || exit
  fi
  if [[ -n "${GRAPH_EVIDENCE_DIR:-}" ]]; then
    export PROJECT DEMO_ROOT GRAPH_EVIDENCE_DIR
    python - <<'SAVE_GRAPH' || exit
import json, os
from pathlib import Path
out = Path(os.environ['GRAPH_EVIDENCE_DIR'])
assert out.is_dir(), 'STOP: graph evidence directory does not exist; do not create a replacement.'
manifest = Path(os.environ['DEMO_ROOT'], 'operator-evidence/graph-source.json')
if manifest.exists():
    x = json.loads(manifest.read_text())
    assert x.get('project') == os.environ['PROJECT'], 'STOP: graph source belongs to another project.'
SAVE_GRAPH
    export RAG_GRAPH_SAVED_PROJECT="$PROJECT"
  else
    unset RAG_GRAPH_SAVED_PROJECT
  fi
  temporary=$(mktemp "$root/.rag-resume.XXXXXXXX") || exit
  trap 'rm -f -- "$temporary"' EXIT
  chmod 600 "$temporary" || exit
  while IFS= read -r key; do
    if [[ ${!key+x} ]]; then printf 'export %s=%q\n' "$key" "${!key}" || exit; fi
  done < <(_rag_session_keys) > "$temporary" || exit
  mv -f -- "$temporary" "$root/rag-resume.env" || exit
  trap - EXIT
  printf 'PASS: saved nonsecret restart settings to %s\n' "$root/rag-resume.env"
)

rag_restore_api_url() {
  local service revision revision_json url
  unset API_URL DOCUMIND_ID_TOKEN DOCUMIND_OUTSIDER_TOKEN
  service=$(gcloud run services describe documind-api --project="$PROJECT" --region="$REGION" --format=json) || return
  revision=$(python -c 'import json,sys; s=json.load(sys.stdin); r=[x for x in s.get("status",{}).get("traffic",[]) if x.get("percent",0)>0]; assert len(r)==1 and r[0]["percent"]==100 and r[0].get("revisionName"), "STOP: API traffic is split or unresolved; inspect routing."; print(r[0]["revisionName"])' <<<"$service") || return
  revision_json=$(gcloud run revisions describe "$revision" --project="$PROJECT" --region="$REGION" --format=json) || return
  url=$(python -c 'import json,sys; r=json.load(sys.stdin); e={v["name"]:v.get("value", "") for v in r["spec"]["containers"][0].get("env",[])}; u=e.get("SELF_URL", ""); assert u.startswith("https://"), "STOP: serving API revision has no usable SELF_URL."; print(u)' <<<"$revision_json") || return
  export API_URL="$url"
  printf 'Serving revision: %s\nAPI_URL=%s\n' "$revision" "$API_URL"
}

rag_refresh_demo_tokens() {
  local member_token outsider_token
  [[ -n "${PROJECT:-}" && -n "${API_URL:-}" ]] || { _rag_stop 'Restore PROJECT and the API audience URL first.'; return 1; }
  unset DOCUMIND_ID_TOKEN DOCUMIND_OUTSIDER_TOKEN
  local -a args=(auth print-identity-token --include-email "--audiences=$API_URL")
  member_token=$(gcloud "${args[@]}" "--impersonate-service-account=documind-ui-sa@$PROJECT.iam.gserviceaccount.com") || return
  outsider_token=$(gcloud "${args[@]}" "--impersonate-service-account=documind-outsider-sa@$PROJECT.iam.gserviceaccount.com") || return
  [[ -n "$member_token" && -n "$outsider_token" ]] || { _rag_stop 'An identity-token command returned no token.'; return 1; }
  export DOCUMIND_ID_TOKEN="$member_token" DOCUMIND_OUTSIDER_TOKEN="$outsider_token"
  printf 'PASS: both identity tokens refreshed.\n'
}

rag_check_resumed_api() {
  rag_refresh_demo_tokens || return
  local endpoint
  for endpoint in health ready version; do
    printf '\nGET /%s\n' "$endpoint"
    curl --fail-with-body -sS --max-time 60 -H "Authorization: Bearer $DOCUMIND_ID_TOKEN" --write-out '\nHTTP %{http_code}\n' "$API_URL/$endpoint" || return
  done
  printf 'PASS: API health, readiness and version requests succeeded.\n'
}

# One local environment for the operator commands; service images retain their own manifests.
_rag_operator_python_path() {
  local root="${RAG_SESSION_HOME:-$HOME}"
  printf '%s\n' "$root/rag-shell-venv/bin/python"
}

_rag_check_operator_interpreter() {
  local root="${RAG_SESSION_HOME:-$HOME}" executable active
  executable="$(_rag_operator_python_path)" || return
  [[ -x "$executable" ]] || { _rag_stop 'Python environment is missing. Complete Python 3.12 setup, then run rag_install_python_dependencies.'; return 1; }
  active="$(type -P python)" || { _rag_stop 'Activate rag-shell-venv before running local operator commands.'; return 1; }
  "$executable" - "$root/rag-shell-venv" "$active" <<'RAG_INTERPRETER'
import sys
from pathlib import Path
expected, active = map(Path, sys.argv[1:])
if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != expected.resolve():
    raise SystemExit('STOP: the required Python 3.12 virtual environment is not selected. Complete the runbook Python environment setup.')
if active.absolute().parent != expected.absolute() / 'bin':
    raise SystemExit('STOP: python on PATH is outside rag-shell-venv. Activate that environment and run hash -r before retrying.')
print('Python:', sys.executable)
RAG_INTERPRETER
}

rag_check_python_dependencies() {
  [[ $# == 0 ]] || { _rag_stop 'Usage: rag_check_python_dependencies'; return 2; }
  [[ -n "${DEMO_ROOT:-}" ]] || { _rag_stop 'Restore DEMO_ROOT before checking Python dependencies.'; return 1; }
  _rag_check_operator_interpreter || return
  local executable
  executable="$(_rag_operator_python_path)" || return
  "$executable" - "$DEMO_ROOT" <<'RAG_DEPENDENCIES' || return
import importlib
import importlib.metadata
import re
import sys
from pathlib import Path
root = Path(sys.argv[1])
files = ('shared/requirements.txt', 'services/ingest/requirements.txt',
         'services/rag-api/requirements.txt', 'services/mcp/requirements.txt')
expected = {'rank-bm25': '0.2.2'}
errors = []
for relative in files:
    path = root / relative
    if not path.is_file():
        errors.append(f'missing {relative}; complete the runbook Git bulk-copy step first')
        continue
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s;]+)', line)
        if not match:
            errors.append(f'{relative}:{number}: expected an exact dependency pin; review this manifest')
            continue
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        version = match[2]
        if name in expected and expected[name] != version:
            errors.append(f'conflicting source pins for {name}: {expected[name]} and {version}')
        expected[name] = version
for name, version in sorted(expected.items()):
    try:
        actual = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        errors.append(f'{name}=={version} is not installed')
    else:
        if actual != version:
            errors.append(f'{name}: installed {actual}, source requires {version}')
# Import real package entry points and the SDK symbols used by the runbook. No clients are created.
checks = {
    'google.auth': ('default',),
    'google.auth.transport.requests': ('Request',),
    'requests': ('Session',),
    'google.cloud.firestore': ('Client',),
    'google.cloud.firestore_v1.base_query': ('FieldFilter',),
    'google.cloud.firestore_v1.vector': ('Vector',),
    'google.cloud.firestore_v1.base_vector_query': ('DistanceMeasure',),
    'google.cloud.storage': ('Client',),
    'google.cloud.aiplatform': ('MatchingEngineIndex', 'MatchingEngineIndexEndpoint'),
    'google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint': ('HybridQuery', 'Namespace'),
    'vertexai': ('init',),
    'vertexai.rag': ('create_corpus',),
    'google.genai': ('Client',),
    'google.cloud.discoveryengine_v1': ('SearchServiceClient', 'RankServiceClient'),
    'google.cloud.spanner': ('Client',),
    'google.cloud.bigquery': ('Client',),
    'google.cloud.documentai': ('DocumentProcessorServiceClient',),
    'google.cloud.dlp': ('DlpServiceClient',),
    'pydantic': ('BaseModel',),
    'pydantic_settings': ('BaseSettings',),
    'pypdf': ('PdfReader',),
    'PIL.Image': ('open',),
    'numpy': ('array',),
    'fastmcp': ('Client',),
    'fastmcp.client.transports': ('StreamableHttpTransport',),
    'rank_bm25': ('BM25Okapi',),
    'httpx': ('Client',),
    'tiktoken': ('get_encoding',),
}
for module, symbols in checks.items():
    try:
        imported = importlib.import_module(module)
        for symbol in symbols:
            getattr(imported, symbol)
    except Exception as exc:
        errors.append(f'{module}: {type(exc).__name__}: {exc}')
if errors:
    print('STOP: operator Python dependencies are incomplete or inconsistent.', file=sys.stderr)
    for error in errors:
        print('  - ' + error, file=sys.stderr)
    raise SystemExit('Complete Git bulk-copy if needed, then run rag_install_python_dependencies. This is not an ADC login error.')
print(f'PASS: {len(expected)} source-pinned packages and {len(checks)} operator imports are available.')
RAG_DEPENDENCIES
  "$executable" -m pip check || { _rag_stop 'pip check found an inconsistent environment. Run rag_install_python_dependencies and resolve its reported conflict before continuing.'; return 1; }
  printf 'PASS: local Python dependencies are ready; cloud access is checked separately.\n'
}

rag_install_python_dependencies() {
  [[ $# == 0 ]] || { _rag_stop 'Usage: rag_install_python_dependencies'; return 2; }
  local root="${RAG_SESSION_HOME:-$HOME}" executable relative
  [[ -n "${DEMO_ROOT:-}" ]] || { _rag_stop 'Restore DEMO_ROOT and complete the Git bulk-copy step first.'; return 1; }
  [[ -s "$root/rag-shell-venv/bin/activate" ]] || { _rag_stop 'Complete the Python 3.12 environment setup first.'; return 1; }
  source "$root/rag-shell-venv/bin/activate" || return
  hash -r
  _rag_check_operator_interpreter || return
  executable="$(_rag_operator_python_path)" || return
  local -a requirements=()
  for relative in shared/requirements.txt services/ingest/requirements.txt services/rag-api/requirements.txt services/mcp/requirements.txt; do
    [[ -s "$DEMO_ROOT/$relative" ]] || { _rag_stop "Missing $relative. Complete Git bulk-copy before installing operator dependencies."; return 1; }
    requirements+=(-r "$DEMO_ROOT/$relative")
  done
  "$executable" -m pip install "${requirements[@]}" numpy 'rank-bm25==0.2.2' || {
    _rag_stop 'Operator dependency installation failed. Resolve the pip error above; do not continue to tenant or demo commands.'
    return 1
  }
  rag_check_python_dependencies
}

rag_resume() {
  local root="${RAG_SESSION_HOME:-$HOME}" key helper shell_only=false
  if [[ $# == 1 && $1 == --shell-only ]]; then shell_only=true
  elif [[ $# != 0 ]]; then _rag_stop 'Usage: rag_resume [--shell-only]'; return 2; fi
  test -s "$root/rag-resume.env" || { _rag_stop 'No saved operator session. Restore the original setup and run rag_save_session once.'; return 1; }
  ( source "$root/rag-resume.env" && _rag_verify_base_snapshot ) || return
  while IFS= read -r key; do unset "$key"; done < <(_rag_session_keys)
  source "$root/rag-resume.env" || return
  source "$root/rag-source-session.env" || return
  export RAG_SESSION_FILE="$root/rag-source-session.env"
  export DEPLOY_ROOT="$DEMO_ROOT" PYTHONPATH="$DEMO_ROOT" PROJECT_ID="$PROJECT"
  export GOOGLE_CLOUD_PROJECT="$PROJECT" GOOGLE_CLOUD_QUOTA_PROJECT="$PROJECT"
  export CLOUDSDK_CORE_PROJECT="$PROJECT" CLOUDSDK_BILLING_QUOTA_PROJECT="$PROJECT"
  export TF_VAR_project_id="$PROJECT" TF_VAR_region="$REGION"
  if [[ -n "${OPERATOR_EMAIL:-}" ]]; then export CLOUDSDK_CORE_ACCOUNT="$OPERATOR_EMAIL"; fi
  [[ -s "$root/rag-shell-venv/bin/activate" && -s "$root/rag-git-source.sh" ]] || { _rag_stop 'Saved Python environment or Git helper is missing; restore shell setup.'; return 1; }
  source "$root/rag-shell-venv/bin/activate" || return
  hash -r
  rag_check_python_dependencies || return
  _rag_add_cli_directory "$root/.local/bin"
  if ! _rag_check_terraform WARNING; then
    printf 'API-only resume can continue. Terraform-dependent checkpoints require rag_require_terraform to pass first.\n' >&2
  fi
  source "$root/rag-git-source.sh" || return
  cd "$DEMO_ROOT" || return
  hash -r
  unset RAG_REPAIR_DOCUMENT_EMBEDDINGS RAG_SMOKE_REQUIRED_VERSION RAG_SMOKE_VERSION DOCUMIND_ID_TOKEN DOCUMIND_OUTSIDER_TOKEN API_URL
  for helper in rag-wait-source.sh rag-upload-all-text-pdf.sh; do
    if [[ -s "$root/$helper" ]]; then source "$root/$helper" || return; fi
  done
  gcloud auth print-access-token >/dev/null || { _rag_stop 'CLI authentication failed. Sign in as the intended operator, then rerun rag_resume.'; return 1; }
  if ! python - <<'PY'
import os, google.auth
from google.auth.transport.requests import Request
credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'], quota_project_id=os.environ['PROJECT'])
credentials.refresh(Request())
print('PASS: Python ADC refreshed for quota project ' + os.environ['PROJECT'])
PY
  then
    _rag_stop 'Python ADC failed. Complete application-default login and set-quota-project, then retry.'; return 1
  fi
  printf 'PASS: restored project %s and original source snapshot %s\n' "$PROJECT" "$SOURCE_COMMIT"
  if [[ "$shell_only" == true ]]; then
    printf 'Shell-only resume complete. Continue the interrupted setup step; no API lookup or deployment was performed.\n'
    return 0
  fi
  rag_restore_api_url && rag_check_resumed_api || return
  printf 'Resume the interrupted checkpoint. For the graph demonstration, run rag_resume_graph.\n'
}

rag_resume_graph() {
  local root="${RAG_SESSION_HOME:-$HOME}" assignments
  [[ -n "${PROJECT:-}" && -n "${DEMO_ROOT:-}" && -n "${RAG_SOURCE_REPO:-}" ]] || { _rag_stop 'Run rag_resume first.'; return 1; }
  if [[ -z "${GRAPH_EVIDENCE_DIR:-}" && -s "$root/rag-graph-resume.env" ]]; then
    assignments=$(_rag_legacy_graph_exports) || return
    eval "$assignments"
  fi
  [[ -n "${GRAPH_EVIDENCE_DIR:-}" ]] || { _rag_stop 'No saved graph evidence path. Restore the original C1 path; do not create a new directory.'; return 1; }
  if [[ -n "${RAG_GRAPH_SAVED_PROJECT:-}" && "$RAG_GRAPH_SAVED_PROJECT" != "$PROJECT" ]]; then
    _rag_stop 'Saved graph settings belong to another project.'; return 1
  fi
  export PROJECT DEMO_ROOT GRAPH_EVIDENCE_DIR
  if [[ ! "${PROJECT_NUMBER:-}" =~ ^[0-9]+$ ]]; then
    PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)') || return
    export PROJECT_NUMBER
  fi
  _rag_verify_graph_snapshot || return
  assignments=$(_rag_graph_exports) || return
  eval "$assignments"
  printf 'PASS: restored graph evidence directory %s; original API/pin backups were retained.\n' "$GRAPH_EVIDENCE_DIR"
  if [[ ! -s "$GRAPH_EVIDENCE_DIR/acme-pin-before.json" ]]; then
    printf 'Resume C1 schema/pin checkpoint, then C2/C3. The original Acme pin has not yet been saved.\n'
  elif [[ ! -e "$GRAPH_EVIDENCE_DIR/ask-spanner.json" ]]; then
    printf 'No successful C3 result exists yet. Resume C2 if the graph build was interrupted, otherwise C3.\n'
  elif [[ -z "${GRAPH_SEED_K:-}" ]]; then
    printf 'C3 question/distance restored. Restore the original GRAPH_SEED_K explicitly before C4; it is not recorded in ask-spanner.json.\n'
  else
    printf 'C3 evidence and seed settings restored. Resume C4 setup, graph-off requests, graph-on requests, then validation.\n'
  fi
  printf 'This command did not change traffic, rebuild the graph, fetch source or replace the parent source snapshot.\n'
}
rag_promote_ready_service() {
  test "$#" -eq 1 || return 2
  : "${PROJECT:?Restore PROJECT}" "${REGION:?Restore REGION}"
  python - "$1" "$PROJECT" "$REGION" <<'PY'
import json, shlex, subprocess, sys, time

service, project, region = sys.argv[1:]
flags = [f'--project={project}', f'--region={region}']
identity = generation = target = revision_identity = None
last_service = last_revision = None
last_report = None

def stop(message):
    print('STOP: ' + message, file=sys.stderr)
    if last_revision:
        print('Candidate revision conditions: ' + json.dumps(last_revision.get('status', {}).get('conditions', [])), file=sys.stderr)
    print(shlex.join(['gcloud', 'run', 'services', 'describe', service] + flags + [
        '--format=yaml(metadata.generation,status.observedGeneration,status.latestCreatedRevisionName,status.latestReadyRevisionName,status.conditions,status.traffic)']), file=sys.stderr)
    if target:
        print(shlex.join(['gcloud', 'run', 'revisions', 'describe', target] + flags + ['--format=yaml(metadata,status.conditions)']), file=sys.stderr)
        query = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" AND resource.labels.revision_name="{target}"'
        print(shlex.join(['gcloud', 'logging', 'read', query, f'--project={project}', '--freshness=24h', '--limit=50', '--format=json']), file=sys.stderr)
    raise SystemExit(1)

def command(args, timeout=30):
    try:
        result = subprocess.run(['gcloud', 'run'] + args + flags + ['--format=json'],
                                check=True, stdout=subprocess.PIPE, text=True, timeout=timeout)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            stop('Cloud Run returned an unexpected response; inspect the operation before retrying.')
        return value
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        stop(f'Cloud Run command failed: {exc}. An update may already have taken effect; inspect status before retrying.')

def conditions(data):
    return {c.get('type'): c for c in data.get('status', {}).get('conditions', [])}

def reconciled(data):
    g = data.get('metadata', {}).get('generation')
    o = data.get('status', {}).get('observedGeneration')
    return g is not None and o is not None and str(g) == str(o)

def traffic_at_target(data, desired=False):
    rows = data.get('spec' if desired else 'status', {}).get('traffic', [])
    return (sum(int(r.get('percent') or 0) for r in rows if r.get('revisionName') == target) == 100
            and not any(int(r.get('percent') or 0) > 0 and r.get('revisionName') != target for r in rows))

def service_snapshot(after_update=False):
    global identity, generation, last_service
    last_service = data = command(['services', 'describe', service])
    meta = data.get('metadata', {})
    uid, current = meta.get('uid'), meta.get('generation')
    if not uid or current is None:
        stop('Service identity or generation is missing; inspect the service response.')
    if identity is None:
        identity, generation = uid, str(current)
    if uid != identity:
        stop('Service identity changed; do not continue this promotion.')
    if not after_update and str(current) != generation:
        stop('Another service update occurred during this check; inspect the deployment and retry.')
    if target and data.get('status', {}).get('latestCreatedRevisionName') != target:
        stop('A different revision was created during this check; inspect before continuing.')
    return data

def revision_snapshot():
    global revision_identity, last_revision
    last_revision = data = command(['revisions', 'describe', target])
    meta = data.get('metadata', {})
    if meta.get('name') != target or not meta.get('uid'):
        stop('Candidate revision identity is missing or does not match the selected revision.')
    if revision_identity is None:
        revision_identity = meta['uid']
    if revision_identity != meta['uid']:
        stop('Candidate revision identity changed during promotion.')
    if reconciled(data):
        for kind, c in conditions(data).items():
            reason = c.get('reason', '').lower()
            failed = (kind in ('Ready', 'ContainerReady', 'ContainerHealthy')
                      or (kind == 'ResourcesAvailable' and reason not in ('retired', 'retiring', 'reserve')))
            if c.get('status') == 'False' and failed:
                stop(f'Candidate revision reports {kind}=False: {c.get("reason", "")} {c.get("message", "")}')
    return data

def report(stage, data, revision=None):
    global last_report
    state = {'stage': stage, 'target': target, 'generation': data.get('metadata', {}).get('generation'),
             'observed_generation': data.get('status', {}).get('observedGeneration'),
             'latest_ready': data.get('status', {}).get('latestReadyRevisionName'),
             'traffic': data.get('status', {}).get('traffic', []),
             'revision_conditions': list(conditions(revision or {}).values())}
    if state != last_report:
        print(json.dumps(state, indent=2), flush=True)
        last_report = state

def active_revision(data):
    cs = conditions(data)
    ready = cs.get('Ready', {})
    if not (reconciled(data) and ready.get('status') == 'True'
            and ready.get('reason', '').lower() not in ('retired', 'retiring', 'reserve')
            and cs.get('Active', {}).get('status') == 'True'):
        return False
    return all(cs[k].get('status') == 'True' for k in ('ResourcesAvailable', 'ContainerHealthy', 'ContainerReady') if k in cs)

# Pin the current deployment, then inspect that revision instead of an old latestReady.
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    current = service_snapshot()
    if reconciled(current):
        created = current.get('status', {}).get('latestCreatedRevisionName')
        if created:
            target = created
            revision = revision_snapshot()
            report('before activation', current, revision)
            if reconciled(revision) and conditions(revision).get('Ready', {}).get('status') == 'True':
                break
    time.sleep(5)
else:
    stop('Timed out waiting for the selected revision readiness. No traffic was changed.')

# Retired means inactive infrastructure, not a failed image. Assign traffic to reactivate it.
revision = revision_snapshot()
if not (reconciled(revision) and conditions(revision).get('Ready', {}).get('status') == 'True'):
    stop('Candidate readiness changed before promotion; inspect and retry.')
current = service_snapshot()
if not reconciled(current):
    stop('Service reconciliation changed before promotion; inspect and retry.')
traffic_updated = not (traffic_at_target(current) and traffic_at_target(current, desired=True) and active_revision(revision))
if traffic_updated:
    try:
        subprocess.run(['gcloud', 'run', 'services', 'update-traffic', service] + flags +
                       [f'--to-revisions={target}=100'], check=True, timeout=180)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stop(f'Traffic update failed: {exc}. Inspect its outcome before retrying.')

# Routing may activate infrastructure asynchronously; prove readiness AND actual traffic.
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    current = service_snapshot(after_update=traffic_updated)
    revision = revision_snapshot()
    report('after activation', current, revision)
    ready = conditions(current).get('Ready', {})
    if reconciled(current) and ready.get('status') == 'False':
        stop('Service reports Ready=False after traffic activation: ' + json.dumps(ready))
    if (str(current['metadata']['generation']) != generation and reconciled(current)
            and not traffic_at_target(current, desired=True)):
        stop('Desired traffic changed away from the selected revision during activation; inspect routing.')
    if (reconciled(current) and ready.get('status') == 'True'
            and traffic_at_target(current, desired=True) and traffic_at_target(current) and active_revision(revision)):
        confirmed = service_snapshot(after_update=traffic_updated)
        if not (reconciled(confirmed) and conditions(confirmed).get('Ready', {}).get('status') == 'True'
                and traffic_at_target(confirmed, desired=True) and traffic_at_target(confirmed)):
            time.sleep(5)
            continue
        print(f'PASS: {service} revision {target} is active, Ready and receives 100% of traffic.')
        break
    time.sleep(5)
else:
    stop('Timed out after traffic activation. Inspect conditions and routing before retrying.')
PY
}
rag_promote_ready_api() {
  rag_promote_ready_service documind-api
}

