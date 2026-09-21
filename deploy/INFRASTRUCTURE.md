# Infrastructure: first run and safe repeat runs

Run these commands from the deployment directory (`deploy/` in a checkout, or
`$HOME/deploy_module_rag` in the workstation). The deployment files are ready to
run from Git. Students do not need to generate them from lesson files or paste
Terraform replacements into the terminal.

## What is fixed

- `terraform/dataplex.tf` enables the Dataplex API, explicitly creates or reuses
  its Google-managed service identity, grants `roles/dataplex.serviceAgent`, and
  only then creates the data-quality scan. API enablement alone did not guarantee
  that the service account existed in a new project. The existing scan ID, BigQuery
  tables and GKE resources are unchanged.
- `terraform/managed.tf` declares the existing default digital parser explicitly.
  Removing an implicit `document_processing_config` used to propose replacing
  Acme and Zeta's search stores on a second plan. Their names, IDs, data and schema
  remain the same. `prevent_destroy` blocks an accidental replacement; it is not
  an instruction to delete or recreate a store.
- `terraform/budget.tf` discovers the project's linked billing account when
  `billing_account_id` is omitted or empty. A supplied nonempty ID is still
  validated. This reads billing linkage; it never changes which account pays for
  the project. The Cloud Billing API and read permission are still required.
- `commands/infrastructure.py` saves confirmed project, billing and CI inputs,
  passes them explicitly to Terraform, checks the resulting plan and applies
  only a checked saved plan. It uses Python's standard library, `gcloud` and
  Terraform; no extra Python package is needed.
- Downloading code from the public learners repository does not change which
  GitHub repository may deploy through Workload Identity Federation. Existing
  CI trust is read from the deployment and retained. A new CI identity must be
  specified deliberately.

## 1. Update source once, preserving local deployment data

Use the `prod_agent` branch of the public learners repository for an anonymous
download of the deployment kit. It was created from `rag-production-hardening`
with the course module folders removed. The original main-repository course
branch is `claude/rag-production-hardening`.

For an existing workstation deployment, this copies the current committed
deployment source into the existing directory. It does not delete untracked
files, `.terraform`, state, saved `.tfvars`, evidence or downloaded PDFs.
It does replace tracked source files with the Git version, so first retain any
intentional local source edits you need.

```bash
export DEMO_ROOT="$HOME/deploy_module_rag"
RAG_UPDATE_DIR="$(mktemp -d "$HOME/documind-source-update.XXXXXX")" &&
git clone --depth 1 --single-branch --branch prod_agent \
  https://github.com/netsetos/agentic-ai-weekend-gcp-learners.git "$RAG_UPDATE_DIR" &&
mkdir -p "$DEMO_ROOT" &&
cp -a "$RAG_UPDATE_DIR/deploy/." "$DEMO_ROOT/"
```

Keep the printed clone revision with the run's evidence:

```bash
git -C "$RAG_UPDATE_DIR" rev-parse HEAD
```

An old runbook's saved `SOURCE_COMMIT` still points at the old source. Repeating
its old `rag_get_file` commands can restore the defect. Use this updated deploy
directory and its Git-owned commands for infrastructure recovery. If restarting
the entire application build, establish a fresh runbook source snapshot at the
updated revision; do not silently mix an old source record with new app code.

## 2. Restore the existing project, credentials and state

```bash
export PROJECT="your-existing-project"
export REGION="asia-south1"
cd "$DEMO_ROOT"
gcloud config set project "$PROJECT"
```

Keep the project's existing ADC and quota-project setup from the bootstrap
steps. If ADC is missing, log in before setting its quota project:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"
```

Use the same state bucket, prefix and workspace that created the resources.
For an already initialized deployment, inspect them; do not rerun `init` with
some other guide's default prefix:

```bash
terraform -chdir=terraform workspace show
terraform -chdir=terraform state list
```

For a fresh directory or deliberately restored backend, explicitly initialize
with the known settings after enabling APIs and preparing the state bucket:

```bash
terraform -chdir=terraform init -input=false \
  -backend-config="bucket=${TFSTATE_BUCKET:?Set the intended state bucket}" \
  -backend-config="prefix=${TFSTATE_PREFIX:?Set the exact state prefix}"
```

Do not use `-migrate-state`, `state rm`, `destroy` or an empty replacement backend
to resolve this parsing/billing problem. If the state list is unexpectedly empty
for an existing project, restore its actual backend/workspace before continuing.

## 3. Prepare and plan

For an existing deployment, the following command reads billing and existing CI
trust, saves confirmed inputs, validates Terraform and creates/checks a new plan:

```bash
python commands/infrastructure.py plan --project "$PROJECT" --region "$REGION"
```

For a genuinely new CI deployment only, select the repository that will run your
deployment workflow. This example authorizes the main repository's course branch
even if the source was downloaded anonymously from the learners repository:

```bash
python commands/infrastructure.py prepare --project "$PROJECT" --region "$REGION" \
  --github-repository netsetos/agentic-ai-weekend-gcp \
  --github-repository-id 1358872052 \
  --deploy-ref refs/heads/claude/rag-production-hardening
python commands/infrastructure.py plan --project "$PROJECT" --region "$REGION"
```

If `prod_agent` GitHub Actions are intentionally the deployment authority on a new
project, use the learners repository, ID `1367035480`, and
`refs/heads/prod_agent` as the three explicit values. This is an explicit opt-in,
not a requirement for a public clone or a change made by creating the branch.
Existing deployments preserve their current trust;
the helper refuses a conflicting selection instead of performing a CI migration.

Keep all your other intended Terraform feature inputs. Existing `.tfvars` and
`TF_VAR_` values still supply noncritical settings. The confirmed critical values
are stored in `terraform/runbook-project.auto.tfvars.json` and passed last with
`-var-file`, preventing an old empty billing export or learners CI export from
overriding the selected project configuration. The file is local to your project
and excluded from Git.

Review the full plan. For the reported parser/billing issue, the Acme and Zeta
replacements must disappear. The checked path refuses every deletion/replacement,
including a GKE mode switch, and refuses changes to existing CI trust. It is not a
migration tool. New resources and ordinary updates still need your review.

## 4. Check and apply the selected plan

After reviewing the successful plan:

```bash
python commands/infrastructure.py check --project "$PROJECT" --region "$REGION"
python commands/infrastructure.py apply --project "$PROJECT" --region "$REGION"
```

The helper records the successful plan and its project/backend/workspace context.
It repeats the checks before apply. It does not generate a replacement plan
inside `apply`. An interrupted apply or a stale-plan error requires a new `plan`,
review and `apply`; keep the same state so successfully created resources are
retained.

After a successful apply, rerun the plan command. Expect `No changes` for these
repairs. Any remaining diff is a separate change to investigate.

The infrastructure commands do not upload documents or redeploy Cloud Run
services. Continue your existing service, ingestion and retrieval steps only
after infrastructure is ready. `make plan` uses the checked planning path;
`make up` applies the selected plan before continuing its broader service setup.

## After a workstation restart

Restore `PROJECT`, `REGION`, your deployment directory, CLI PATH and credentials.
The input and selected-plan records remain on disk. Start with the `plan` command
again if an apply was interrupted or you are unsure whether state changed. No
shell-defined billing or promotion function is needed for these infrastructure
commands.

## Dataplex service account missing after a partial apply

If the scan reports that `service-PROJECT_NUMBER@gcp-sa-dataplex.iam.gserviceaccount.com`
does not exist, update the deployment source from Git using step 1 above. The
identity and its role are now managed in Terraform; no local Terraform edits or
replacement service account are needed.

Keep the same project, backend and workspace. Run a new `plan`, review it, and run
`apply` using steps 3 and 4. Do not reuse the plan from the interrupted apply.
Terraform retains successfully created resources recorded in that state, including
the node pool. This repair should add the service-identity/IAM prerequisites and
the missing scan, with no resource deletions or replacements.

Google Cloud can still take a few minutes to propagate a newly created service
identity or its IAM grant. If the same error persists immediately after their
successful creation, allow a few minutes, then create and review another plan
before retrying. Repeated permission errors need investigation; a longer scan
timeout alone does not fix a missing identity or grant.

## Verification and limits

```bash
python -m unittest discover -s commands/tests -p 'test_infrastructure.py'
```

The regression suite uses fake CLI responses and never contacts GCP. It checks
billing failures, CI preservation, input precedence, failed plans and destructive
plan refusal. Native Terraform changes must also be validated/planned against
your project. Permissions, quotas, API enablement and service readiness are live
requirements; offline checks cannot certify them.

`prevent_destroy` also blocks deliberate data-store teardown while the resource
declaration remains. Plan any eventual cleanup separately and account for indexed
documents, retention and data-store ID reuse delays.
