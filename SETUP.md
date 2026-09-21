# Set up the production agent branch

Use this guide for **`prod_agent`**, which runs the committed code under
`deploy/`. Start with offline validation, then prepare GCP when you are ready
to deploy. Python 3.12 matches the repository's CI environment.

## 1. Clone and create a Python environment

```bash
git clone --branch prod_agent --single-branch https://github.com/netsetos/agentic-ai-weekend-gcp-learners.git
cd agentic-ai-weekend-gcp-learners
python -m venv .venv
```

Activate the environment on macOS/Linux:

```bash
source .venv/bin/activate
```

Or in Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the offline gate dependencies, matching the CI workflow:

```bash
python -m pip install pydantic pandas pypdf requests
```

For a service you run locally, also install its own requirements from
`deploy/services/<service>/`. The root `requirements.txt` retains course pins
and notebook tooling; it is not the production stack's combined requirements.

## 2. Run the offline checks

Run from the repository root:

```bash
python -m compileall -q deploy
python -m unittest discover -s deploy/commands/tests -v
python -m unittest discover -s deploy/operators/tests -v
python -m unittest discover -s deploy/evals/tests -v
python deploy/extract_documind.py --check
python deploy/validate.py
python deploy/evals/run_eval.py
```

`make -C deploy dryrun` is a shortcut for the extraction check, validator and
offline evaluation; run the three regression suites above as well.

These checks do not need GCP credentials. Extraction reports a **kit-only
checkout**, since there are no curriculum source notebooks to compare against.
The validator skips Terraform, tflint and Docker checks if the tools are
unavailable. Install Terraform 1.9.8 (the CI version) and Docker for infrastructure
validation and image builds; provider and image downloads require internet
access. Review skips and warnings as well as the exit code.

## 3. Prepare GCP for deployment

Use Bash, Cloud Shell or a Cloud Workstation for the deployment commands. You
will need the Google Cloud CLI, Terraform, Make, the intended GCP project with
billing enabled, and permission to use its resources.

For an existing project:

```bash
export PROJECT="your-project-id"
export REGION="us-central1"
gcloud config set project "$PROJECT"
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT"
```

Use your deployment's existing region when resuming it. `PROJECT` is the
Makefile input; `.env.example` uses `PROJECT_ID`, and the Makefile does not
automatically load that file.

Install the client used by the roster command:

```bash
python -m pip install google-cloud-firestore==2.30.0
```

Enable the deployment APIs through the committed command:

```bash
make -C deploy apis PROJECT="$PROJECT"
```

Follow [deploy/INFRASTRUCTURE.md](deploy/INFRASTRUCTURE.md) to select or restore
the correct Terraform backend, preserve existing CI trust, prepare inputs and
review a saved plan before applying it. A new project also needs a Terraform
state bucket; reuse the correct bucket and prefix for an existing deployment.
Check prerequisites with the intended bucket:

```bash
make -C deploy preflight PROJECT="$PROJECT" REGION="$REGION" TFSTATE_BUCKET="your-state-bucket"
```

Then follow [deploy/README.md](deploy/README.md) for service deployment,
ingestion, live evaluation and smoke checks. The `prod_agent` branch does not
automatically gain deployment permissions; release automation uses the
repository/ref configured in Workload Identity Federation.

## Costs and shutdown

Live infrastructure, model calls, document processing and GPU workloads can
incur charges. Set a project budget and review the deployment guide's shutdown
commands. `make off` reduces running costs but does not remove every billable
resource; inspect the guide before choosing a shutdown or teardown operation.
