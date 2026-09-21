# DocuMind — Production Agent

The **`prod_agent`** branch of Netsetos's DocuMind project contains the deployable
RAG and agent stack: document ingestion, retrieval and generation, chat, MCP,
A2A, a frontend, and infrastructure on Google Cloud.

This branch starts from `rag-production-hardening` at
`b844ef4dbf2bdd250f90e16aadbd89ad995b310f`. The 13 course module folders have been
removed so production development can focus on the committed code under
`deploy/`. Course notebooks remain on the
[`rag-production-hardening` branch](https://github.com/netsetos/agentic-ai-weekend-gcp-learners/tree/rag-production-hardening).
Course lessons and interview material are available at [netsetos.com](https://netsetos.com).

## Repository layout

| Path | Contents |
|---|---|
| `deploy/services/` | RAG API, ingestion, chat, MCP, A2A agent, admin, frontend and model-serving services |
| `deploy/shared/` | Shared contracts, tenancy, authorization, corpus and tool code |
| `deploy/terraform/` | Infrastructure definitions |
| `deploy/evals/` | Corpus, golden set and offline/live evaluation tools |
| `deploy/commands/`, `deploy/operators/`, `deploy/smoke/` | Deployment commands, maintenance tools and smoke checks |
| `docs/` | HTML guides to the Firestore architecture, document updates and idempotency |
| `.github/workflows/` | Offline validation and manually triggered release workflow |
| `requirements.txt`, `.env.example` | Retained course dependency pins and example settings; service dependencies live with each service |

## Get started

```bash
git clone --branch prod_agent --single-branch https://github.com/netsetos/agentic-ai-weekend-gcp-learners.git
cd agentic-ai-weekend-gcp-learners
python -m venv .venv
source .venv/bin/activate
python -m pip install pydantic pandas pypdf requests
make -C deploy dryrun
```

For Windows PowerShell, virtual environment activation, the full regression
commands and GCP preparation, follow [SETUP.md](SETUP.md).

The offline gate does not call GCP. Terraform validation and container builds
run when the relevant tools are installed; provider and image downloads can
require internet access. The notebook extraction check reports a kit-only
checkout because this branch uses the committed deployment files directly.

## Working on the stack

Edit the committed files under `deploy/` and run the offline checks before
publishing changes. Each service has its own requirements file and Dockerfile;
the root course requirements are not a complete service environment.

- [Deployment guide](deploy/README.md): service commands, local chat and validation.
- [Infrastructure guide](deploy/INFRASTRUCTURE.md): project/backend setup and the reviewed plan/apply sequence.
- [Document indexing guide](deploy/INDEXING.md): ingestion, source versions, recovery and reindexing.
- [Architecture overview](docs/index.html): storage and lifecycle explanations.
- [Curriculum provenance](deploy/INDEX.md): maps inherited files to the original course lessons.

Creating or pushing this branch does not deploy to GCP. The release workflow is
manual, and cloud deployment still requires the intended project, credentials,
Terraform state and explicitly configured CI trust. Live evaluation and smoke
checks are part of the deployment process.

## Questions

Project and course questions: sart@netsetos.com.
