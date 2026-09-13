# Incremental indexing - the document lifecycle on the lane

How a document update is handled in DocuMind, written so another team can adopt it without DocuMind. Built on
12 September 2026 on top of the ledger of 11 September (`services/ingest/`, `terraform/`, `evals/`, `smoke/`,
`shared/documind_corpus.py`); the plan it comes from is *Incremental Indexing on the Lane* (the handoff folder).
The rule of the page: an update should **cost what changed, be invisible until it is whole, be reversible for
free, be provable by the same gates as a release, and leave a trail a person can read.**

## 1. Eight principles

Each one is a question a reviewer can ask of any RAG index, whatever the store, and the answer this kit gives.

| # | Principle | Here |
|---|---|---|
| P1 | **Three identities.** A source (the object path), a version (the hash of its bytes), a chunk (the hash of its text plus a locator that survives an insertion above it). | `sources/{tenant~path}`; `doc_key = tenant_sha256`; every row carries `chunk_hash` and `locator` (`NP-03`, `p7-1`, `preamble`, `figure`, `t0-60`). The chunk **id** stays `tenant:sha256#i`: stable for citations and the golden set (decision D2). |
| P2 | **Detect change by hash and generation, never by time.** Same hash, nothing to do. An event older than the ledger's generation is a late redelivery. | `stale_generation()` before the download; the bytes are fetched **by generation**; `ingest_stale_event` acks it. `contracts.is_stale()` is the pure rule. |
| P3 | **Pay for what changed; pin what you paid with.** Reuse every vector whose chunk hash is unchanged; stamp the embedding model and version on every row. | `embed_with_carry_over()`: 281 of the handbook's 283 chunks reused after a one-clause edit, 2 embedded. `EMBEDDING_MODEL@EMBEDDING_VERSION` from **one** Terraform variable on the worker and the API. |
| P4 | **Visibility is a swap.** Write the new version staged and invisible, flip in one pass; the reader keeps the newest version per source on its own. | `mirror_to_firestore(staged=True)` then `swap_versions()` (new to current first, then old to retired, 400 writes a batch); `retriever.newest_per_source()` inside `prefer_current()`. |
| P5 | **Retire, never delete; purge by policy.** A retired row stays for the retention window; a TTL policy on the store removes it, declared in Terraform, executed by the platform. | `expire_at = superseded_at + retention_days`; `google_firestore_field.chunks_expire_at` with `ttl_config {}`. **Nothing on the lane calls delete.** `reactivate` clears the stamp. |
| P6 | **Reconcile on a schedule and measure the drift.** The event path is the fast path; the nightly walk is what makes a lost event a delay instead of a hole, and it must emit a number. | `reconcile.py` as a `google_cloud_run_v2_job` (`reconcile.tf`), 23:30 IST; `reconcile_done` carries `drift`; `documind/reconcile_drift` alerts above zero for two nights. |
| P7 | **A reindex is a release.** The offline gate (the rows move with the document), the live gate scoped to the rows that cite the source, on a candidate, then a person. | `make reindex` runs `run_eval.py` first; the `version` shape (`vr-01`); `run_eval.py --source` / `make eval-live SOURCE=`; `smoke_reindex.py` in `make smoke-all`. |
| P8 | **Observe the lifecycle; let the cache follow the ledger.** Events become metrics and alerts; a versions view exists; the cache is keyed to a fingerprint of the current versions. | `documind/ingest_events` / `ingest_embedded` / `ingest_reused` / `reconcile_drift`; `GET /v1/sources`, the UI's Documents page, `make sources`; `ledger/{tenant}.fingerprint` vs `tenant_caches.corpus_fingerprint` (`cache_stale`). |

## 2. The update path, step by step

One object under `gs://PROJECT-uploads/<tenant>/<name>` changes (same name, new bytes). Cloud Storage publishes
`object.finalized` with the object's **generation**; the push subscription delivers it to `documind-ingest`.

1. **Guard.** `stale_generation()` reads `sources/{tenant~name}`. An event older than the recorded generation
   is acked with `ingest_stale_event` and nothing else happens. Otherwise the bytes of *that generation* are
   downloaded; a generation that no longer exists is the same case (its successor's event indexes the object).
2. **Claim.** `documents/{tenant_sha256}` in a transaction; the row carries `tenant_id` (the MCP server filters
   on the field, never on the id's prefix). A refused claim whose status is `superseded` is the undo (step 7); one
   whose status is `queued` is acked as `queued_batch` (the batch lane holds it, step 3); any other refusal is a
   duplicate, acked.
3. **Parse, scan, chunk.** A PDF is counted by pypdf *before* Doc AI sees it; over `MAX_INLINE_PAGES` (250) it
   goes to the batch lane - `ingest_batch/{doc_key}` written with the object, its generation and its type, the
   claim set to `queued`, `ingest_queued_batch` logged with the consumer named - and is indexed from there by the
   **batch job** (`batch.py`, 13 September 2026): `documind-ingest-batch`, a Cloud Run job on the same image with
   no request deadline (`batch.tf`), started by the worker as it queues (`BATCH_JOB`) and hourly regardless, which
   takes the claim in a transaction (`take_batch`), fetches the bytes *by generation* and runs the same pipeline as
   step 4 onward (`index_document`, `lane=batch`); `make batch` runs it now, `make queued` lists the queue, and a
   claim the job fails stays `failed` with its reason (section 8). Anything else is
   counted by the parser. Text is chunked by **section** when it has `## ` headings (a handbook: one chunk per
   clause, the clause code as the locator), otherwise fixed 2,000-character windows with a 200 overlap, cut
   page by page so a window never crosses a form feed (`p7-1`; a mirror's `<!-- -->` provenance header is
   dropped). Every chunk gets `chunk_hash` (sha256 of its whitespace-collapsed text). The same two rules live in
   `shared/documind_corpus.py`, so a notebook mints the same chunk texts, hashes and locators - the gate holds a
   29-page mirror to 65 identical chunks from either chunker (12 September 2026). One DLP scan per document.
4. **Carry-over.** The previous version's current rows of this source are read by hash; a vector is reused only
   when its `embedding_model@embedding_version` is the configured one. The misses are embedded. `reused` and
   `embedded` are the counts every later line carries.
5. **Stage.** The new rows are written with `current=false, staged=true` and a one-day `expire_at` (a stage nothing
   ever swaps leaves by policy). No reader can see them.
6. **Swap.** `swap_versions()` flips the new rows current (clearing the stage marks), then retires every other
   current row of the source: `current=false`, `superseded_by`, `superseded_at`, `expire_at = now + RETENTION_DAYS`,
   `effective_to` when the successor declares a date. On the full profile the ANN tier follows: the new datapoints
   go up after the swap, the retired ids come out. Then the **managed mirror** (P9.2, 13 September 2026), when
   `MANAGED_MIRROR` names a store: the version's text - the same text these rows hold - goes to the tenant's RAG
   Engine corpus (4.3) and / or Vertex AI Search data store (4.4) as one document named by its `doc_key`, and the
   versions the swap retired leave them; `mirror_ok` per store, `mirror_failed` on an error (the ingest is not
   failed - the walk repairs the mirror), `mirror_no_store` once for a tenant without one, `mirror_skipped` for a
   media version, which stays here (`services/ingest/managed.py`). Off on the lane; refused unless `RESIDENCY=us`.
7. **The undo, verified.** The same bytes again, after a newer version retired them. First the tombstone: a
   source a person withdrew (`make retire`) is acked as `ingest_withdrawn` and nothing moves. Then `reactivate()`
   counts before it flips - the retired rows still here against the claim's `chunks`, the claim's `retired_at`
   against `RETENTION_DAYS`. A shortfall or a closed window is `reactivate_incomplete` (both numbers on the line),
   nothing is flipped, and the worker takes the claim back and ingests the bytes as a fresh version (step 3 on;
   the carry-over reuses what the newer version still holds). Otherwise the rows come back with their stamps
   cleared, on the full profile their ids go back up from the rows' own vectors (`reupsert`) *before* the newer
   version is retired in turn, and nothing is embedded (`ingest_reactivated`); the managed mirror follows the undo
   - the reactivated version's text read off its own rows, the newer version deleted (`after_undo`). The first
   undo flipped whatever
   remained and retired the newer version regardless; after the window, that left a source with no current
   version at all.
8. **Record.** `documents/` (chunks, reused, embedded, generation), `sources/` (the ledger row: doc_key,
   generation, sha256, chunks, reused, embedded, retired, effective_from, embedding stamp), then
   `ledger/{tenant}.fingerprint` = sha256 of the tenant's sorted current doc_keys. `ingest_ok` (and
   `ingest_superseded` when something was retired) carries all of it; `doc.upload` goes to the audit trail.

### The ledger's states

| Row | `status` | Set by | Means | Leaves by |
|---|---|---|---|---|
| `sources/` | `indexed` | `record_source` | the current version is live | a re-issue (a new `doc_key`, still `indexed`), `make retire`, the object leaving the bucket |
| `sources/` | `retired` | the nightly walk; `make restore` | the object left the bucket; its rows are flagged and expiring | the object back in the bucket: the next walk plans a reingest |
| `sources/` | `withdrawn` | `make retire` | a person took it down; the object is **kept**; its rows are flagged and expiring | `make restore` only - never the walk (*withdrawn, object kept*), never a redelivery, never the same bytes again (`ingest_withdrawn`) |
| `documents/` | `processing` | `claim`, `take_batch` | a worker, or the batch job, holds this version | `finish`, `release`, or the batch hand-off |
| `documents/` | `queued` | the batch hand-off | over `MAX_INLINE_PAGES`; waiting for the batch job | `take_batch` (`processing`) when the job runs - started by the worker, hourly, or `make batch`; the walk reports it and moves on |
| `documents/` | `indexed` | `finish`, `reactivate` | the version is current | the swap (`superseded`) |
| `documents/` | `superseded` | the swap, `make retire`, the walk | retired at `retired_at`; the undo window runs from it | `reactivate` (`indexed`), or a retaken claim after a refused undo (`processing`) |
| `documents/` | `failed` | `release` | the error is on the row | the next delivery's claim |

Deleting the object and `make retire` are two different states on purpose: the first is storage absence, which
the walk repairs the moment the object is back; the second is a decision, which nothing repairs but a person.

## 3. The reader

`retrieve()` asks Firestore (or Vector Search) for the tenant's nearest chunks, with `current == true` as a
pre-filter when `RETRIEVAL_CURRENT_ONLY=on` (the second vector index in `firestore_indexes.tf`). Whatever the
switch, `prefer_current()` drops retired rows and then keeps **one version per source** (the newest `indexed_at`
or `reactivated_at`), before the reranker. The context header carries `effective from D` and the generator adds
the dated rule only when a packed source has a date; the SSE citation event carries `effective_from`. The
cache: `generate_config_kwargs()` compares the record's `corpus_fingerprint` with `ledger/{tenant}` in one read and
runs uncached on a mismatch (`cache_stale`), until `make cache` packs the corpus that changed.

## 4. Fields

| Where | Field | Written by | Meaning |
|---|---|---|---|
| `chunks/{id}` | `doc_key`, `current`, `indexed_at` | worker, loader | the version, the flag, when it landed (schema 1) |
| `chunks/{id}` | `chunk_hash`, `locator`, `section` | worker, loader | the chunk's identity across versions (schema 2) |
| `chunks/{id}` | `embedding_model`, `embedding_version`, `schema_version` | worker, loader | what the vector was made with; the row's shape |
| `chunks/{id}` | `staged`, `expire_at` | worker, loader | invisible until swapped; the TTL policy's field (retired and staged rows only) |
| `chunks/{id}` | `superseded_by`, `superseded_at`, `effective_to`, `reactivated_at` | swap, reactivate | the retirement, and the undo |
| `documents/{doc_key}` | `status`, `tenant_id`, `chunks`, `reused`, `embedded`, `generation` | claim, finish | the per-version claim, whose it is, and what it cost |
| `documents/{doc_key}` | `retired_at`, `queued_at` | swap, retire, the batch hand-off | the undo window's clock; the wait for the batch lane |
| `sources/{tenant~name}` | `doc_key`, `generation`, `sha256`, `chunks`, `reused`, `embedded`, `retired`, `effective_from`, `status`, `embedding_*` | record_source | the ledger: what is current for a path, since when, at what cost |
| `sources/{tenant~name}` | `withdrawn_at`, `restored_at` | `make retire`, `make restore` | the tombstone, and when it was lifted |
| `ledger/{tenant}` | `fingerprint`, `versions`, `last_event` | refresh_fingerprint | the corpus identity both caches follow: 10.2's context cache and, since 12 September, 12.6's answer cache |
| `tenant_caches/{tenant}` | `corpus_fingerprint` | cache_admin | what the pack was made from |

## 5. Terraform

| File | What | Why |
|---|---|---|
| `firestore_indexes.tf` | `google_firestore_field.chunks_expire_at` with `ttl_config {}` | the only deleter (P5) |
| `variables.tf` | `retention_days` (30), `embedding_model`, `embedding_version` + outputs | one declared number, one declared embedding; `make deploy-services` reads the outputs into both services |
| `reconcile.tf` | `google_cloud_run_v2_job.reconcile` on `var.reconcile_image`, IAM, the 23:30 IST schedule; `RECONCILE_JOB=true` | the whole night is one apply (P6). Needs an ingest image first: `make build`, then `make reconcile-job` |
| `alerts.tf` | metrics `documind/ingest_events` (label `event`), `ingest_embedded`, `ingest_reused`, `reconcile_drift`; policies *Ingest failed* (10 min), *Ledger drift above zero for two nights*, *Nightly reconcile failed* | the lifecycle is a pager, not a log search (P8) |

## 6. The operator's playbook

| Situation | Command | What you read back |
|---|---|---|
| One document changed | `make reindex FILE=<new> NAME=<same name> TENANT=<t>` (the offline gate runs first), then move the golden rows it turned red, then `make eval-live SOURCE=<name> API=<candidate>` | `ingest_ok` with `reused`, `embedded`, `retired`; `ingest_superseded`; the scoped gate green |
| Only metadata changed | nothing | the night's reconcile records the generation: *touch* |
| A late redelivery of an old version | nothing | `ingest_stale_event`; the ledger untouched |
| The same version from the notebook lane (4.1's Document AI ingester, 2.3 / 4.2 / 4.5's `seed()`), uploaded to the bucket | nothing | `ingest_already_current`: the claim recorded with the rows already current, the ledger learns the generation, nothing parsed or embedded. Both lanes name a version by the sha of the OBJECT (a real Act's PDF, never its mirror), so neither re-issues the other's rows; the other order needs nothing - `seed()` skips a version the lane holds (13 September 2026) |
| The change was wrong | `make reindex FILE=<old> NAME=<same name>` | `ingest_reactivated`, nothing embedded - inside the `RETENTION_DAYS` undo window; after it, `reactivate_incomplete` then `ingest_ok` (a fresh version, the carry-over paying only for what changed) |
| A document withdrawn on purpose | `make retire SOURCE=` | `reconcile_withdrawn`: the ledger row `withdrawn`, the object kept, the rows expiring after `RETENTION_DAYS`; every night the plan says *withdrawn, object kept* and does nothing; the same bytes again are `ingest_withdrawn` |
| A document deleted from the bucket | delete the object | retired by the night's walk (`reconcile_retired`, the row `retired`); put the object back and the next walk re-ingests it |
| Bring a withdrawn document back | `make restore SOURCE=` | `reconcile_restored`, then `ingest_reactivated` inside the undo window or `ingest_ok` after it; refused with the reason when the source is not withdrawn or its object is gone |
| A document over 250 pages | nothing - it is queued and the batch job indexes it (`make batch` to run the job now, `make queued` to see the queue; `make batch-job` once, to declare it) | `ingest_queued_batch` with the consumer named, then the job's `ingest_ok` with `lane=batch` and the record in `ingest_batch/` (`indexed`, or `failed` with the reason); the plan's `queued` line until then, not drift |
| The graph after a reindex | `make graph TENANT=` | `graph_built` with nodes and edges; only chunks whose `chunk_hash` changed are re-extracted (`graph_extractions/`), the rest is cached |
| Many documents changed | `make ingest-corpus`, `make reconcile APPLY=1` | per-source counts; `reconcile_done` with `drift` 0 the night after |
| Which version is live? | `make sources TENANT_ONLY=acme`, the UI's Documents page, `GET /v1/sources?tenant_id=` | every source's version, generation, counts, dates, the fingerprint |
| Is the cache current? | `make cache CACHE_OP=show` | *current*, or *STALE* with both fingerprints |
| A lane without the TTL policy | `make purge` (prints), `make purge APPLY=1` | the rows the policy would have removed; on a lane with the policy, nothing |
| Does the lifecycle work at all? | `make smoke-reindex` (in `make smoke-all`) | v2 in: reindexed with the counts, the answer moved; v1 in: reactivated, the answer back |

## 7. The gates

- **Offline, every PR** (`run_eval.py`): the `version` shape - `must_contain` from the current version of its
  `source`, `must_not_contain` a figure only a retired version under `evals/demo` holds. Re-issue the handbook
  without moving `lk-06` and `vr-01` and the gate is red before anything deploys.
- **Wiring** (`tools/check_auth_wiring.py`): the chunker measured on the handbook's two revisions (281 of 283
  reused), the carry-over plan, the newest-per-source guard, the fake-Firestore swap / reactivate / fingerprint,
  the TTL field, the job, the metrics, the Makefile and the deploy scripts.
- **Lifecycle** (`tools/check_lifecycle.py`): the tombstone (a withdrawn source is never re-ingested by the plan,
  never reactivated), the verified undo (a shortfall and a closed window refuse, flip nothing, log both numbers),
  the batch claim left `queued` and skipped by the plan, the job's take and run (`take_batch`, `batch.py`: by
  generation, the worker's own pipeline, a gone generation and a failed document recorded), `tenant_id` on the
  claim, the `doc_type` restrict and the re-upsert - against the same fake Firestore.
- **Live, on a candidate** (`make eval-live SOURCE=`): the rows that cite the document; a version row that cites a
  retired figure blocks on its own; a threshold with no rows in scope is reported, not judged.
- **Smoke** (`make smoke-reindex`): the lifecycle end to end on a three-chunk fixture.

## 8. What is deliberately not done

- No in-place overwrite of a chunk, ever; a re-issue is new rows beside retired ones.
- No delete by any account or cron; `--purge` prints unless `--apply`, and exists for a lane without the policy.
- No versions inside the shared `Citation` contract (decision D5): the version rides on the row, the header,
  the stream and the UI.
- No second ingestion path for updates: the reconcile re-ingests by rewriting the object onto itself.
- The batch lane's consumer is a job, not a second service (13 September 2026): `documind-ingest-batch` runs
  `batch.py` on the ingest image with no request deadline, started by the worker as it queues a document
  (`BATCH_JOB`, `_run_batch_job`) and hourly at :15 regardless (`batch.tf`, behind `BATCH_JOB=true` like the
  reconcile job). It takes each queued claim in a transaction, fetches the bytes by generation and runs the
  worker's own `index_document()`; two runs never index one document twice. A document it fails stays `failed`
  with its reason, and the job never retakes it: `make reindex`, or the same bytes uploaded again, is the way back.
- The managed mirror (P9.2) is one-directional and eventually consistent: the worker, the batch job, the walk's
  retirement and `make retire` write to the tenant's stores (`managed.py`), an import is an operation the stores
  finish in minutes, and nothing reads the stores back yet - `make managed-status` compares each store with the
  ledger by hand, and the walk's `mirror_missing` / `mirror_stale` / `mirror_orphan` actions are P9.3. The
  `rag_engine` and `vertex_search` retrieval backends that read the stores are P9.4 and P9.5.
- The graph (4.6) is built by hand, not by the worker: `make graph TENANT=` extracts over the tenant's current
  chunks (cached by `chunk_hash`, so a re-issued document costs its changed chunks only) and loads `graph_nodes` /
  `graph_edges` with the lesson's own `FirestoreGraph` (`shared/documind_graph.py`). A version swap leaves a node's
  `chunk_ids` pointing at retired rows until the next `make graph`; the retriever drops those the way it drops every
  retired chunk (`prefer_current`, the `current` check), so a stale graph loses coverage, never correctness.
- Not yet (the strategy's P2): `make reembed EMBEDDING_VERSION=` (a full re-embed into new rows behind a
  candidate), an index per embedding version on the full profile, an as-of filter on `effective_to`.

## 9. How another team follows this

1. Can you name a source, a version and a chunk, and does the chunk identity survive an insertion above it?
2. Is change detected by hash and ordered by generation, with a late redelivery ignored?
3. Does an edit of one paragraph embed one paragraph, and is the embedding model stamped on every row?
4. Can a reader ever retrieve two versions of one source? If the write is not a swap, does the reader guard?
5. Is anything deleted by a person or a cron, or only by a declared retention policy?
6. Does a scheduled reconcile run, emit a drift number, and alert when it stays non-zero?
7. Does a document change go through the same gates as a code change, with the test set moving in the same commit?
8. Are the lifecycle events metrics with alerts, is there a versions view, and is the cache keyed to the current versions?

Where each answer lives here: `services/ingest/` (the path), `services/rag-api/retriever.py` and
`cache_manager.py` (the reader), `terraform/` (the policies), `evals/` and `smoke/` (the proof),
`shared/documind_corpus.py` (the notebooks' copy of the rules), and lessons 4.1, 4.2, 4.5, 4.7, 4.8, 12.2, 12.3,
12.5, 12.7, 12.8 and 13.1 to 13.3 (the teaching).
