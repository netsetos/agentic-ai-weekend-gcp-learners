from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RETRIEVAL_BACKENDS = ("vector", "firestore")
RETRIEVAL_MODES = ("dense", "hybrid")


def check_retrieval_modes(backend: str, mode: str) -> None:
    """RETRIEVAL_MODE against RETRIEVAL_BACKEND, at startup (12 September 2026, R06). Hybrid is Vector Search's
    HybridQuery (4.5's hybrid.py); Firestore's vector index takes one dense vector and nothing else, so on the lean
    profile RETRIEVAL_MODE=hybrid ran dense while every usage row and /version said hybrid. A service that cannot do
    what its environment says must not start: the message names the fix, so it is read on the failed deploy and not
    found in the rows a week later. An unknown value is refused for the same reason - a typo ran dense too."""
    if backend not in RETRIEVAL_BACKENDS or mode not in RETRIEVAL_MODES:
        raise ValueError(f"RETRIEVAL_BACKEND={backend!r} RETRIEVAL_MODE={mode!r}: the backend is one of "
                         f"{'|'.join(RETRIEVAL_BACKENDS)} and the mode one of {'|'.join(RETRIEVAL_MODES)}")
    if backend == "firestore" and mode == "hybrid":
        raise ValueError("RETRIEVAL_MODE=hybrid needs RETRIEVAL_BACKEND=vector: the Firestore backend (the lean profile) "
                         "is dense-only. Set RETRIEVAL_MODE=dense, or deploy the full profile with a Vector Search "
                         "endpoint and keep hybrid.")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    project_id: str = Field(alias="GOOGLE_CLOUD_PROJECT")
    region: str = "us-central1"
    india_region: str = "asia-south1"
    # Which store answers the vector query. `vector` is Vector Search with the Firestore
    # fallback beneath it (the chaos rung). `firestore` - the kit's lean profile - is
    # Firestore's own vector index alone: no endpoint to keep warm, the same tenant
    # pre-filter, the ANN tier left out. Nothing else in the service changes.
    retrieval_backend: str = Field("vector", alias="RETRIEVAL_BACKEND")   # vector | firestore
    # The ledger (12.5, 11 September 2026): `on` retrieves only chunks the ledger marks current - one
    # version per document. Off until the second vector index is built and the chunks written before
    # the ledger carry the field (make backfill-current); a switch, judged on a candidate like the others.
    retrieval_current_only: str = Field("off", alias="RETRIEVAL_CURRENT_ONLY")   # off | on
    vector_index_endpoint: str = Field("", alias="VECTOR_INDEX_ENDPOINT")
    vector_deployed_index: str = Field("", alias="VECTOR_DEPLOYED_INDEX_ID")
    # THE Firestore collection. Gap G2: 2.3 wrote `knowledge_base`, 4.2/4.5/4.6 read
    # `rag_chunks`, and this service read `chunks` - three names for one corpus, so the
    # teaching lane and the production lane never saw the same documents. Every notebook
    # from 2.3 onward now names this one. services/ingest/indexer.py writes to it.
    chunks_collection: str = "chunks"
    # ONE declared embedding (12 September 2026): EMBEDDING_MODEL and EMBEDDING_VERSION are variables.tf's
    # embedding_model / embedding_version, set on this service AND on the ingest worker by make deploy-services,
    # so the query vector and the document vectors come from one model by construction. The worker stamps the
    # pair on every chunk row; a bump is a planned migration (make reembed, deploy/INDEXING.md), never a silent
    # mismatch. /version reports it beside the model and the prompt.
    embed_model: str = Field("text-embedding-005", alias="EMBEDDING_MODEL")
    embedding_version: str = Field("1", alias="EMBEDDING_VERSION")
    # GENERATOR_MODEL in the environment. A model NAME is served on the global endpoint; a tuned model is
    # an ENDPOINT path (projects/.../locations/us-central1/endpoints/...) and is served on a regional
    # client - generator.py picks by the value (10.1: tuning is regional). RAG_MODEL_BASE names the base
    # a tuned endpoint was tuned from, for pricing (cost.py) and for the cache's model check.
    generator_model: str = "gemini-3.6-flash"
    rag_model_base: str = Field("", alias="RAG_MODEL_BASE")
    # Where a tuned endpoint is served from. Empty: read from the endpoint path (its /locations/<x>/ segment -
    # the first live tuning job put its endpoint in the `us` multi-region, not us-central1, F41). Set it to
    # force a location (global, us-central1) without a code change - a setting, like the model.
    generator_location: str = Field("", alias="GENERATOR_LOCATION")
    # 10.3, behind a flag: ROUTING=on classifies each question (router.py) and lets the budget breaker
    # (breakers.py) pick the tier; off, the generator model above serves everything. The spend the
    # breaker reads is the month's counter in Firestore (budget.py) over BUDGET_USD; SPEND_PCT
    # overrides it for a replay ("what does 85% look like").
    routing: str = Field("off", alias="ROUTING")
    budget_usd: float = Field(100.0, alias="BUDGET_USD")
    spend_pct_override: str = Field("", alias="SPEND_PCT")
    rerank_model: str = "semantic-ranker-fast-004"
    # The Ranking API's deadline (12 September 2026): past it, or on any error, rerank() returns the pool by retrieval
    # score and the row says rerank_fallback=1. Generous beside a 20-record call, which answers well inside a second;
    # a ranker that takes longer is not ranking, it is down, and a worse order beats a hung request and then a 500.
    rerank_timeout_s: float = Field(5.0, alias="RERANK_TIMEOUT_S")
    # The pool the reranker sees - the funnel's width. 20 shipped; evals/ablate.py's "dense 50 -> rerank 5" arm is
    # the measurement that moves it, and the row's rerank_ms / pool columns are what the move costs. An env var, so
    # the move is a number in the service's environment, not a code change. top_k (the request, <= 20) is what comes OUT.
    top_k_retrieve: int = Field(20, alias="TOP_K_RETRIEVE")
    top_k_rerank: int = 5
    max_context_tokens: int = 8000
    # 2048, not 1024: a statute answer with its quotes - and the thinking drawn from the same
    # budget on the 3.x family - outran 1024 on fourteen rows of the second live eval, and each
    # cut-off JSON was scored as a refusal. generator.py retries once with three times this.
    max_answer_tokens: int = 2048

    # Which backend answered, and which prompt did it. Both go on every log
    # line and every span, because "the answer got worse last Tuesday" is
    # unanswerable without them (12.6 puts them in BigQuery).
    # Module 11: THE BACKEND IS A SETTING. vertex = google.genai (the lane); gateway = 11.3's LiteLLM gateway at
    # LITELLM_URL, where GENERATOR_MODEL names a route (documind-slm is 11.4's self-hosted model). Until Module 11
    # this field was reported on every usage row and switched nothing.
    model_backend: str = Field("vertex", alias="MODEL_BACKEND")          # vertex | gateway
    litellm_url: str = Field("", alias="LITELLM_URL")
    gateway_timeout_s: float = Field(90.0, alias="GATEWAY_TIMEOUT_S")   # a cold GPU behind the gateway takes a while
    # 12.6: Model Armor on both sides of the model, behind a switch. Off by default - the lane does not move; a
    # candidate revision with ARMOR=on is where 12.6 judges it. The template is regional (asia-south1, with the data
    # it inspects); guard.py reads the location and the template name from the same variables at import.
    armor: str = Field("off", alias="ARMOR")                              # off | on
    armor_location: str = Field("asia-south1", alias="ARMOR_LOCATION")
    armor_template: str = Field("documind-guard", alias="ARMOR_TEMPLATE")
    # 12.6's answer cache (semantic_cache.py), wired 12 September 2026: off | on. On, a near-enough earlier question
    # of the same tenant under the same corpus fingerprint is answered from Firestore - no retrieval, no model call.
    # Off on the lane until the threshold is measured on paraphrase pairs (the RAG plan, W4).
    semantic_cache: str = Field("off", alias="SEMANTIC_CACHE")
    prompt_id: str = "documind-rag"
    prompt_version: str = "v3"
    retrieval_mode: str = "dense"          # dense | hybrid (4.5's hybrid.py)

    # USD per 1M tokens, gemini-3.6-flash standard. 12.6 moves this to a
    # BigQuery model_prices table so a rate change is not a redeploy.
    price_in: float = 1.50
    price_out: float = 7.50

    @model_validator(mode="after")
    def _retrieval_modes_agree(self):
        # Refused here, at import, so a revision with an impossible pair never serves: the deploy fails with the
        # message above instead of a service that runs dense and reports hybrid. /version reports the mode that
        # passed this check - the effective one.
        check_retrieval_modes(self.retrieval_backend, self.retrieval_mode)
        return self

settings = Settings()
