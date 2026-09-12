# GenAI on GCP - Learner Code

Companion code repo for the **Netsetos GenAI on GCP Capstone** course (v2.0) at [netsetos.com](https://netsetos.com).

> 13 modules, 68 lessons, one running example: **DocuMind**, a multi-tenant document-answering product on Vertex AI, Firestore and Cloud Run.

## What is in this repo

| Folder | What it contains |
|---|---|
| `module-NN-slug/lesson-N.M-slug/notebooks/` | the lesson's runnable Colab notebook, with an Open in Colab badge at the top |
| `module-NN-slug/lesson-N.M-slug/practice/` | the practice-lab notebook, where the lesson has one (43 of 68) |
| `module-NN-slug/lesson-N.M-slug/interview/` | a placeholder; the interview Q&A lives on netsetos.com |
| `requirements.txt` | the pins for running the notebooks locally |
| `.env.example` | the two variables you set locally |

> Lesson pages, practice-lab walkthroughs and interview Q&A live on [netsetos.com](https://netsetos.com). This repo is code only:
> open a notebook, run it, change it.

## One project: the kit

Every notebook builds the same product, and from lesson 2.3 on it reads the product's code from one place: the kit under
`deploy/` in [netsetos/agentic-ai-weekend-gcp](https://github.com/netsetos/agentic-ai-weekend-gcp), branch `feat/lesson-4.8-live-evals`.
The notebooks clone it for you (`/content/agentic-ai-weekend-gcp` on Colab) and take the corpus, the answer contract and
the services from that tree; Module 12 ships it. Lesson 1.1 shows the layout. There is no second project to keep in step.

## Getting started

### Colab (recommended)

1. Open a lesson's `notebooks/` folder and click the **Open in Colab** badge at the top of the notebook.
2. Change `PROJECT_ID = "documind-ai-YOUR-ID"` to your own project id.
3. Run the cells in order: the first cell installs the pins, the second authenticates (`auth.authenticate_user()`).

[SETUP.md](SETUP.md) is the one-time GCP project setup (the same steps as lesson 1.1, about 20 minutes).

### Local Jupyter

```bash
git clone https://github.com/netsetos/agentic-ai-weekend-gcp-learners.git
cd agentic-ai-weekend-gcp-learners

python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux

pip install -r requirements.txt
cp .env.example .env                # then set PROJECT_ID and REGION
gcloud auth application-default login
jupyter lab
```

## Course modules

| # | Module | Folder | Lessons |
|---|---|---|---|
| 1 | Setup & IAM | `module-01-setup-and-iam/` | 1.1 Setting Up Your GCP AI Project, 1.2 IAM & Security for GenAI, 1.3 Your First Gemini Call |
| 2 | Embeddings & Vector Stores | `module-02-embeddings-and-vectors/` | 2.1 Token Economics, 2.2 Embeddings: Text to Vectors, 2.3 Firestore Vector Search, 2.4 AlloyDB pgvector & BigQuery Vector Search |
| 3 | Prompting & Routing | `module-03-prompting/` | 3.1 System Prompts & Generation Config, 3.2 Structured Output & JSON Mode, 3.3 Chain-of-Thought & Model Routing |
| 4 | RAG (DIY -> Managed) | `module-04-rag/` | 4.1 Document AI - OCR, Layout Parser, Form Parser, 4.2 DIY RAG Pipeline, 4.3 Vertex AI RAG Engine, 4.4 Vertex AI Search & Google Search Grounding, 4.5 Context Engineering: Hybrid Retrieval, Reranking & Caching, 4.6 Graph RAG: Tenant-Scoped Knowledge Graph on Spanner Graph, 4.7 Evaluate Retrieval Before You Ship: Golden Set, Faithfulness Gate, 4.8 Evals on the Live Lane: Five Thresholds, the Miss List, and the Troubleshooting Lab |
| 5 | BigQuery ML | `module-05-bigquery-ml/` | 5.1 ML in SQL - CREATE MODEL, 5.2 Time Series & Anomaly Detection, 5.3 LLM in SQL - AI.GENERATE, VECTOR_SEARCH, Embeddings, 5.4 BigQuery to Vertex AI, 5.5 Engineer Retrieval Features: Chunk Metadata, Restricts & Quality Gates |
| 6 | Gemini Function Calling | `module-06-function-calling/` | 6.1 Gemini Function Calling, 6.2 Complete Calling Loop, 6.3 Parallel Calls & Built-in Tools, 6.4 Rebuild DocuMind's Tool Loop in LangChain: bind_tools, Chroma Dev Lane & DOCUMIND_PROFILE |
| 7 | MCP & Cloud Run | `module-07-mcp-and-cloud-run/` | 7.1 Building FastMCP Server, 7.2 Deploy to Cloud Run with IAM, 7.3 Connect Agent to Remote MCP |
| 8 | Agents (ADK + A2A) | `module-08-agents-and-adk/` | 8.1 Root Agent with ADK, 8.2 Multi-Agent Orchestration, 8.3 Agent Engine: Managed Deployment, 8.4 A2A Protocol, 8.5 Construct DocuMind's LangGraph Brain: StateGraph, Checkpointers & a Summarise Node, 8.6 Integrate Memory, Human-in-the-Loop & MCP Tools, 8.7 The Agent Harness: Three Brains Over One documind_tools.py |
| 9 | Multimodal & Pre-trained APIs | `module-09-multimodal-and-pretrained/` | 9.1 Gemini Multimodal, 9.2 Generative Media, 9.3 Pre-trained APIs, 9.4 DocuMind Media Studio, 9.5 How Vision Models See, 9.6 Multimodal RAG |
| 10 | Tuning, Caching & Evaluation | `module-10-tuning-and-evaluation/` | 10.1 SFT with LoRA, 10.2 Context Caching, 10.3 Batch API + Model Routing, 10.4 Vertex AI Evaluation, 10.5 Fine-Tune Your Own Small Model, 10.6 Teaching a Model to Reason |
| 11 | Self-hosting (Gemma + LiteLLM) | `module-11-self-hosting/` | 11.1 Gemma on Cloud Run L4, 11.2 Custom FastAPI + vLLM, 11.3 Hybrid LiteLLM Gateway, 11.4 Deploy Your Own Model, 11.5 Cloud Run or GKE Autopilot? |
| 12 | Production Deploy (DocuMind) | `module-12-production-deploy/` | 12.1 Infrastructure & Project Setup, 12.2 RAG API Backend, 12.3 Admin Dashboard & Observability, 12.4 Streamlit Frontend, 12.5 Productionize Ingestion: GCS to Eventarc to Pub/Sub to a Cloud Run Worker, 12.6 Guard and Observe DocuMind: Model Armor, gen_ai Spans, per-Tenant INR, 12.7 Keyless CI/CD: Workload Identity Federation, an Eval Gate and a Two-Minute Rollback, 12.8 Integrate the Surfaces: IAP, One Verifier, and a Smoke Test That Can Say No |
| 13 | Capstone: Defend DocuMind | `module-13-capstone-defend-documind/` | 13.1 Kick Off the Capstone: Eight Components, a Published Rubric, and One Lane, 13.2 Build Phase 1: Your Corpus, Your Citations, Your Eval Suite, 13.3 Build Phase 2: Break It on Purpose, Then Price It, 13.4 Build Phase 3: Your Own Pipeline, an SLO You Can Hold, and a Runbook, 13.5 Write the Post-Mortem and Rehearse: the Peer Mock and the Vocabulary, 13.6 Defend DocuMind: the 90-Minute Assessed Defence |

## Conventions every notebook follows

- The `google-genai` SDK, `genai.Client(enterprise=True, ...)`: Gemini 3.x generation on `location="global"`; embeddings,
  tuning and evaluation on `us-central1`; Firestore and the buckets in `asia-south1` (data residency).
- `PROJECT_ID = "documind-ai-YOUR-ID"` is the placeholder everywhere; Application Default Credentials, never an API key.
- Every price is shown in USD and INR at 85; every pip line is pinned.
- Notebooks are committed clean (no outputs). Run them top to bottom.

## Questions

Course direction and content: sart@netsetos.com.
