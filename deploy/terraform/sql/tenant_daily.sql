CREATE OR REPLACE VIEW `documind_observability.tenant_daily` AS
SELECT
  DATE(timestamp, "Asia/Kolkata") AS day,
  jsonPayload.tenant AS tenant,
  -- The dimensions an answer can be sliced by. "It got worse last Tuesday" is
  -- unanswerable without knowing which prompt version and which retrieval mode
  -- served it.
  jsonPayload.surface AS surface,
  jsonPayload.model_backend AS model_backend,
  jsonPayload.prompt_version AS prompt_version,
  jsonPayload.retrieval_mode AS retrieval_mode,
  jsonPayload.modality AS modality,
  -- Which store served the pool (13 September 2026, evening): vector | firestore | rag_engine | vertex_search - the
  -- EFFECTIVE backend per request (a tenant's pin, or the kit's index after the data-region fallback), so cost per
  -- backend is this GROUP BY and not an estimate. The same typing rule as the stage clocks below: run make bq-views
  -- after an API that logs retrieval_backend, managed_chunks and policy_fallback has answered once.
  jsonPayload.retrieval_backend AS retrieval_backend,
  COUNT(*) AS queries,
  COUNTIF(jsonPayload.answerable = false) AS unanswerable,
  SUM(CAST(jsonPayload.tokens_in  AS INT64)) AS tokens_in,
  SUM(CAST(jsonPayload.tokens_out AS INT64)) AS tokens_out,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS INT64), 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS INT64), 100)[OFFSET(95)] AS p95_ms,
  APPROX_QUANTILES(CAST(jsonPayload.latency_ms AS INT64), 100)[OFFSET(99)] AS p99_ms,
  -- Where the time went. p95_ms moved: which stage moved it? The API row clocks each stage on its
  -- own (main.py stage()), so the answer is a column, not a trace hunt. pool is the candidates the
  -- reranker saw - the number TOP_K_RETRIEVE sets and evals/ablate.py decides - read here after it
  -- moves. BigQuery types a sink table's jsonPayload from the rows it has seen: run `make bq-views`
  -- after an API that logs these four has answered once, or the CREATE fails on "Field name
  -- retrieve_ms does not exist" - a missing field is a refusal, never a column of NULLs.
  APPROX_QUANTILES(CAST(jsonPayload.retrieve_ms AS INT64), 100)[OFFSET(95)] AS p95_retrieve_ms,
  APPROX_QUANTILES(CAST(jsonPayload.rerank_ms   AS INT64), 100)[OFFSET(95)] AS p95_rerank_ms,
  APPROX_QUANTILES(CAST(jsonPayload.generate_ms AS INT64), 100)[OFFSET(95)] AS p95_generate_ms,
  ROUND(AVG(CAST(jsonPayload.pool AS FLOAT64)), 1) AS avg_pool,
  -- The Ranking API stood in for by the retrieval order (retriever.rerank's fallback, 12 September 2026): a
  -- day with a number here served degraded answers, and that number is what pages someone.
  SUM(CAST(jsonPayload.rerank_fallback AS INT64)) AS rerank_fallbacks,
  -- The managed stores' share of the pools, and the questions a tenant's data_region sent to the kit's index instead
  -- of a managed store (main.py retrieval_backend_for): a day with policy_fallbacks is a policy working, not a fault.
  SUM(CAST(jsonPayload.managed_chunks AS INT64)) AS managed_chunks,
  SUM(CAST(jsonPayload.policy_fallback AS INT64)) AS policy_fallbacks,
  SUM(CAST(jsonPayload.cached_tokens AS INT64)) AS cached_tokens,
  ROUND(SUM(CAST(jsonPayload.cost_usd AS FLOAT64)), 4) AS cost_usd,
  ROUND(SUM(CAST(jsonPayload.cost_usd AS FLOAT64)) * 85, 2) AS cost_inr
FROM `documind_observability.run_googleapis_com_stdout`
-- BOTH surfaces. Filtering on "query" alone excluded every streamed answer,
-- and 12.4's UI streams - so the primary user path produced no rows and this
-- view stayed empty however many questions anyone asked. "media" is 9.4's
-- Studio (services/rag-api/media.py, gap G8): modality=image, cost_usd per
-- image, no tokens - so media spend per tenant is a GROUP BY, not an estimate.
WHERE jsonPayload.event IN ("query", "stream", "media")
GROUP BY day, tenant, surface, model_backend, prompt_version, retrieval_mode,
         retrieval_backend, modality;
