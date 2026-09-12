-- Module 5's production lane: the feature job. Lesson 5.5 steps 3, 6 and 7, adopted by 12.3
-- (gap G9). Run it with `make features` - `bq query --use_legacy_sql=false < this file` - after
-- terraform has declared the rag_data tables (dataplex.tf); then the Dataplex scan gates the
-- index rebuild. Every statement is a full recompute or a MERGE, so re-running is safe.
--
-- What is different from the notebook, and why:
--   * chunk_source is not a fixture. services/ingest/indexer.py streams one row per chunk it
--     indexes, with the canonical names (text, source_uri, page_start, doc_type) and the DLP
--     verdict it already computed. So doc_type and pii_flag come from the ingest, not from a
--     BigQuery remote model - the expensive features (5.5 steps 4-5) are free here.
--   * index_feed is TRUNCATE + INSERT, not CREATE OR REPLACE: terraform owns its schema so the
--     quality scan can exist before the first job has run.
--   * The regexes are RE2, as BigQuery speaks it. `[\x{0900}-\x{097F}]` is the Devanagari block.

-- ---------------------------------------------------------------- step 3: cheap features
CREATE OR REPLACE TABLE `rag_data.chunk_features_cheap` AS
SELECT
  chunk_id,
  tenant_id,
  source_uri,
  page_start,
  page_end,
  -- Approximate, deliberately: an exact count is an API call per chunk. ~4 characters per
  -- token for English; Hindi runs richer, so this UNDER-counts, which is the safe direction.
  CAST(CEIL(LENGTH(text) / 4) AS INT64)                            AS token_count,
  ARRAY_LENGTH(SPLIT(COALESCE(heading_path, ''), ' > ')) - 1       AS heading_depth,
  REGEXP_CONTAINS(text, r'(?i)\|.*\|.*\||\btable\s+\d')            AS has_table,
  REGEXP_CONTAINS(text, r'(?i)\bfigure\s+\d|\bfig\.\s*\d')
    OR kind IN ('figure', 'table')                                 AS has_figure,
  CASE
    WHEN REGEXP_CONTAINS(text, r'[\x{0900}-\x{097F}]')
     AND REGEXP_CONTAINS(text, r'[A-Za-z]{4,}')                    THEN 'mixed'
    WHEN REGEXP_CONTAINS(text, r'[\x{0900}-\x{097F}]')             THEN 'hi'
    ELSE 'en'
  END                                                              AS language,
  DATE_DIFF(CURRENT_DATE(), COALESCE(last_revised_at, DATE(ingested_at)), DAY) AS freshness_days,
  doc_type,
  pii_flag
FROM `rag_data.chunk_source`;

-- ---------------------------------------------------------------- step 6: the feature table
MERGE `rag_data.chunk_metadata` T
USING (
  SELECT chunk_id, tenant_id, source_uri, page_start, page_end,
         token_count, heading_depth, has_table, has_figure, language, freshness_days,
         doc_type, 1.0 AS doc_type_conf, pii_flag,
         CURRENT_TIMESTAMP() AS featured_at
  FROM `rag_data.chunk_features_cheap`
) S
ON T.chunk_id = S.chunk_id AND T.tenant_id = S.tenant_id
WHEN MATCHED THEN UPDATE SET
  source_uri = S.source_uri, page_start = S.page_start, page_end = S.page_end,
  token_count = S.token_count, heading_depth = S.heading_depth, has_table = S.has_table,
  has_figure = S.has_figure, language = S.language, freshness_days = S.freshness_days,
  doc_type = S.doc_type, doc_type_conf = S.doc_type_conf, pii_flag = S.pii_flag,
  featured_at = S.featured_at
WHEN NOT MATCHED THEN INSERT ROW;

-- The append-only log. 'withheld' is the row an auditor will ask about.
INSERT INTO `rag_data.ingest_events`
  (event_id, chunk_id, tenant_id, event_type, reason, occurred_at)
SELECT GENERATE_UUID(), chunk_id, tenant_id,
       IF(COALESCE(pii_flag, TRUE), 'withheld', 'reindexed'),
       IF(COALESCE(pii_flag, TRUE), 'pii_flag=true', 'features recomputed'),
       CURRENT_TIMESTAMP()
FROM `rag_data.chunk_metadata`;

-- ---------------------------------------------------------------- step 7: the feed, by subtraction
-- Every chunk missing from the feed was removed by a WHERE clause in ONE place, rather than by
-- a filter each retrieval path has to remember. The scan's rule 2 is the regression test that
-- this subtraction still happens.
TRUNCATE TABLE `rag_data.index_feed`;
INSERT INTO `rag_data.index_feed`
  (chunk_id, tenant_id, doc_type, language, token_count, freshness_days, heading_depth, has_table)
SELECT chunk_id, tenant_id, doc_type, language, token_count, freshness_days, heading_depth, has_table
FROM `rag_data.chunk_metadata`
WHERE NOT COALESCE(pii_flag, TRUE)      -- unknown counts as unsafe
  AND token_count >= 32;                -- too short to carry an answer
