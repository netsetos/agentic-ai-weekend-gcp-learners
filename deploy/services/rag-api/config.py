from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    embed_model: str = "text-embedding-005"
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
    top_k_retrieve: int = 20
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
    prompt_id: str = "documind-rag"
    prompt_version: str = "v3"
    retrieval_mode: str = "dense"          # dense | hybrid (4.5's hybrid.py)

    # USD per 1M tokens, gemini-3.6-flash standard. 12.6 moves this to a
    # BigQuery model_prices table so a rate change is not a redeploy.
    price_in: float = 1.50
    price_out: float = 7.50

settings = Settings()
