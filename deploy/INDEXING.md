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
   when its model, version, `RETRIEVAL_DOCUMENT` task stamp and 768-dimensional vector match the configured profile. The misses are embedded. `reused` and
   `embedded` are the counts every later line carries.
5. **Stage.** The new rows are written with `current=false, staged=true` and a one-day `expire_at` (a stage nothing
   ever swaps leaves by policy). No reader can see them.
6. **Swap.** `swap_versions()` flips the new rows current (clearing the stage marks), then retires every other
   current row of the source: `current=false`, `superseded_by`, `superseded_at`, `expire_at = now + RETENTION_DAYS`,
   `effective_to` when the successor declares a date. The ANN tier follows: the new datapoints
   go up after the swap, the retired ids come out. Then the **managed mirror** (P9.2, 13 September 2026), when
   `MANAGED_MIRROR` names a store: the version's text - the same text these rows hold - goes to the tenant's RAG
   Engine corpus (4.3) and / or Vertex AI Search data store (4.4) as one document named by its `doc_key`, and the
   versions the swap retired leave them; `mirror_ok` per store, `mirror_failed` on an error (the ingest is not
   failed - the walk repairs the mirror), `mirror_no_store` once for a tenant without one, `mirror_skipped` for a
   media version, which stays here (`services/ingest/managed.py`). On by default (`make up` declares the data
   stores, creates the corpora and pins acme and zeta to the two backends). Which *tenants* it copies is not
   the deployment's to say (13 September 2026, evening): `tenant_settings/{tenant}.data_region` is - `in` keeps the
   text on these rows, `any` lets a store outside India hold a copy, absent is `in` (`shared/tenancy.py`,
   `make tenant-policy TENANT= DATA_REGION=`). A forbidden store is `mirror_policy_skipped` once per tenant and
   store; a delete is never refused (it is the direction the policy wants); every copy in or out is a `doc.mirror`
   audit event beside `doc.upload`; and the ledger row is stamped `mirrored` (the stores that confirmed the version,
   the region each holds it in) and `data_region`, which `GET /v1/sources` returns.
7. **The undo, verified.** The same bytes again, after a newer version retired them. First the tombstone: a
   source a person withdrew (`make retire`) is acked as `ingest_withdrawn` and nothing moves. Then `reactivate()`
   counts before it flips - the retired rows still here against the claim's `chunks`, the claim's `retired_at`
   against `RETENTION_DAYS`. A shortfall or a closed window is `reactivate_incomplete` (both numbers on the line),
   nothing is flipped, and the worker takes the claim back and ingests the bytes as a fresh version (step 3 on;
   the carry-over reuses what the newer version still holds). Otherwise the rows come back with their stamps
   cleared, their ids go back up into the ANN tier from the rows' own vectors (`reupsert`) *before* the newer
   version is retired in turn. Matching document embeddings are reused; legacy or missing vectors are regenerated and persisted before reuse (`ingest_reactivated`); the managed mirror follows the undo
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
or `reactivated_at`), before the reranker. Every chunk says which rung found it (16 September 2026): `found_by`
is `vector` for an id `find_neighbors` returned and `firestore` for a row Firestore's own index returned - chosen
(`RETRIEVAL_BACKEND=firestore`, a tenant's pin) or fallen into (`vector_search_fallback`, the same stamp). The
answer carries the count as `stages.vector_chunks` beside `stages.retrieval_backend`, the backend chosen for that
request; `usage_row` carries the same. A deployment on `vector` whose answers say `vector_chunks: 0` is answering
from the rung beneath - which is what `make smoke` now refuses to pass. The graph the walk reads lives where
`GRAPH_BACKEND` says (16 September 2026): `firestore` (`graph_nodes` / `graph_edges` beside the chunks, seeded by a name
the question contains) or `spanner` (`spanner.tf`'s `DocuMindGraph`, 4.6's DDL with an `embedding` column, seeded **by
meaning** - the question's embedding against the node names', `GRAPH_SEED_K` nearest within `GRAPH_SEED_DISTANCE`).
`make graph GRAPH_BACKEND=spanner` builds it there with each name's vector; `/version` reports `graph_backend`. The context header carries `effective from D` and the generator adds
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
| The same version from the notebook lane (4.1's Document AI ingester, 2.3 / 4.2 / 4.5's `seed()`), uploaded to the bucket | nothing | `ingest_already_current`: the claim recorded with the rows already current, the ledger learns the generation, nothing parsed or embedded when all embedding stamps match. Unstamped legacy rows are not adopted. Both lanes name a version by the sha of the OBJECT (a real Act's PDF, never its mirror), so neither re-issues the other's rows; the other order needs nothing - `seed()` skips a version the lane holds (13 September 2026) |
| The change was wrong | `make reindex FILE=<old> NAME=<same name>` | `ingest_reactivated`, matching document embeddings reused (legacy vectors repaired) - inside the `RETENTION_DAYS` undo window; after it, `reactivate_incomplete` then `ingest_ok` (a fresh version, the carry-over paying only for what changed) |
| A document withdrawn on purpose | `make retire SOURCE=` | `reconcile_withdrawn`: the ledger row `withdrawn`, the object kept, the rows expiring after `RETENTION_DAYS`; every night the plan says *withdrawn, object kept* and does nothing; the same bytes again are `ingest_withdrawn` |
| A document deleted from the bucket | delete the object | retired by the night's walk (`reconcile_retired`, the row `retired`); put the object back and the next walk re-ingests it |
| Bring a withdrawn document back | `make restore SOURCE=` | `reconcile_restored`, then `ingest_reactivated` inside the undo window or `ingest_ok` after it; refused with the reason when the source is not withdrawn or its object is gone |
| A document over 250 pages | nothing - it is queued and the batch job indexes it (`make batch` to run the job now, `make queued` to see the queue; `make batch-job` once, to declare it) | `ingest_queued_batch` with the consumer named, then the job's `ingest_ok` with `lane=batch` and the record in `ingest_batch/` (`indexed`, or `failed` with the reason); the plan's `queued` line until then, not drift |
| Where a tenant's text may be held | `make tenant-policy TENANT= DATA_REGION=in\|any` (without `DATA_REGION` it prints the current policy; `make roster` set the demo's three) | `data_region=` on the tenant's `tenant_settings` row; from the next swap the mirror obeys it (`mirror_policy_skipped` for a store it forbids, `doc.mirror` for every copy it permits) and the API holds a managed backend against it (`policy_fallback=1` on the row) |
| Which store answers a tenant | `make tenant-backend TENANT= RETRIEVAL_BACKEND=vector\|firestore\|rag_engine\|vertex_search\|default` (`make up` pins acme to the corpus and zeta to the data store) | `retrieval_backend=` on the tenant's row; from the next question the API serves from it - unless the tenant's `data_region` is `in`, when the row says `policy_fallback=1` and the kit's index answered |
| The graph after a reindex | `make graph TENANT=` (`GRAPH_ARGS="--source hr_policy_2026.md"` for one document's graph, deterministically; the corpus otherwise; `GRAPH_BACKEND=spanner` writes it to Spanner Graph with each name's embedding) | `graph_built` with nodes and edges; only chunks whose `chunk_hash` changed are re-extracted (`graph_extractions/`), the rest is cached |
| Many documents changed | `make ingest-corpus`, `make reconcile APPLY=1` | per-source counts; `reconcile_done` with `drift` 0 the night after |
| Is the ANN tier holding the corpus? | `make vector-status` (`make wait-vectors WANT=200` to block until it is) | the index's own `vectorsCount` and the endpoint's deployed index; 0 right after `make up` is correct - vector.tf creates the index empty, the worker fills it |
| The tier is empty and Firestore is not | `make backfill-vectors APPLY=1` (`TENANT_ONLY=` one tenant; without `APPLY=1` it counts) | `backfill_vectors` reuses matching document embeddings; missing, unstamped or incompatible vectors are regenerated and checkpointed in Firestore after ANN upsert |
| Seed the graph walk by meaning | `make graph TENANT= GRAPH_BACKEND=spanner GRAPH_ARGS="--source hr_policy_2026.md"`, then `make candidate GRAPH_BACKEND=spanner RETRIEVAL_GRAPH=auto` | `graph.py --backend spanner --ask "who signs off on a big purchase?"` seeds the CFO with no stored name in the question (`seeded_by: meaning`, each seed's cosine distance); the answer's `stages.graph_chunks` > 0 on the candidate |
| Did the index answer, or the rung beneath it? | any `/v1/query`: `stages.retrieval_backend` and `stages.vector_chunks`; `make smoke` | `vector_chunks > 0` on a `vector` request is the index; 0 is the Firestore rung and one `vector_search_fallback` line in the log |
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
  candidate), an index per embedding version, an as-of filter on `effective_to`.

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


## Repair document embeddings and verify HR retrieval

The 17 September Acme failure retrieved five repeated performance-review clauses while PB-02 was absent
from the model context. Ingestion was complete (283 current chunks); neither that count nor a successful
HTTP response proves retrieval quality. PB-02 says **six months on probation at grade E2**; its 15 days is
notice during probation, a different fact.

The worker formerly omitted `task_type`. Google's text-embedding API defaults an omitted task to
`RETRIEVAL_QUERY`; documents must use `RETRIEVAL_DOCUMENT`, while the API's question embedding remains
`RETRIEVAL_QUERY`. See [Google's task-type documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/text-embeddings-api).
This is a concrete encoding defect; its contribution to a particular deployed retrieval miss must be
measured with the live acceptance check after migration.

`indexer.py` now explicitly requests document embeddings and stamps `embedding_task_type` on canonical
chunk rows. Carry-over, notebook adoption, vector backfill and undo reject the legacy task profile.
Simply rebuilding the worker or uploading unchanged bytes does not migrate existing indexed claims.
`reconcile.py --backfill-vectors` now performs that repair in batches of 100 and preserves text, source
URIs, chunk IDs and generations. A preview reports `needs_document_embedding`; `--apply` embeds stale
rows, upserts ANN, then checkpoints vectors and task stamps in Firestore. Successful batches reuse their
vectors on retry. Failed writes are surfaced, not marked successful. Source-ledger `embedded/reused`
counts still describe the original ingestion, not this maintenance operation.

Run this during an ingestion maintenance window: stop uploads/undo/notebook seeding and wait for active
ingest/batch work to finish. Keep semantic answer caches off for the verification (the runbook default);
existing cache entries are not invalidated by this same-content repair, so expire them before enabling
cache again. Firestore uses last-update preconditions, but ANN and Firestore are not one transaction.
On a concurrent-change failure stop writers and resolve the affected source before retrying. The in-place
repair is intended for this workshop; a production zero-downtime embedding release should use a candidate
index and switch after acceptance. Embedding API usage is billable.

Fetch and deploy the corrected worker from the branch, then repair Acme. The subshell preserves the other
services' existing source stamps and stores the new ingest stamp separately. Terraform resources and the
API image are unchanged. Existing batch/reconcile jobs using ingest code are updated to the same image.

```bash
(
  set -euo pipefail
  : "${DEMO_ROOT:?Restore the deployment directory}"
  : "${PROJECT:?Restore the intended project ID}"
  : "${REGION:?Restore the deployment region}"
  cd "$DEMO_ROOT"
  source "$HOME/rag-shell-venv/bin/activate"
  source "$HOME/rag-git-source.sh"
  mkdir -p operator-evidence
  # Use the prod_agent checkout of netsetos/agentic-ai-weekend-gcp-learners.
  # Set RAG_SOURCE_REPO to that checkout before running this recovery.
  export SOURCE_BRANCH=prod_agent
  : "${RAG_SOURCE_REPO:?Set the prod_agent learners repository checkout}"
  # Preserve the source stamp used by the other deployed services.
  export RAG_SESSION_FILE="$DEMO_ROOT/operator-evidence/document-embedding-source.env"
  rag_refresh_source
  for path in services/ingest/indexer.py services/ingest/idempotency.py services/ingest/reconcile.py commands/tests/test_document_embeddings.py INDEXING.md; do
    rag_get_file "$path"
  done
  python -m unittest discover -s commands/tests -p test_document_embeddings.py

  # Build only the ingest image, from one complete Git snapshot.
  RAG_BUILD_CONTEXT=$(mktemp -d)
  trap 'rm -rf -- "$RAG_BUILD_CONTEXT"' EXIT
  git -C "$RAG_SOURCE_REPO" archive "$SOURCE_COMMIT" deploy/services deploy/shared deploy/cloudbuild.yaml \
    | tar -x -C "$RAG_BUILD_CONTEXT" --strip-components=1
  RAG_REPAIR_IMAGE="$REGION-docker.pkg.dev/$PROJECT/documind/ingest:$SOURCE_COMMIT"
  gcloud builds submit "$RAG_BUILD_CONTEXT" --project="$PROJECT" --region="$REGION" \
    --config="$RAG_BUILD_CONTEXT/cloudbuild.yaml" \
    --service-account="projects/$PROJECT/serviceAccounts/sa-documind-cicd@$PROJECT.iam.gserviceaccount.com" \
    --gcs-source-staging-dir="gs://$PROJECT-rag-build-source/source" \
    --substitutions="_IMAGE=$RAG_REPAIR_IMAGE,_DOCKERFILE=services/ingest/Dockerfile"
  gcloud run services update documind-ingest --project="$PROJECT" --region="$REGION" \
    --image="$RAG_REPAIR_IMAGE"
  # Upgrade any existing batch/reconcile consumers of the same ingest code.
  gcloud run jobs list --project="$PROJECT" --region="$REGION" --format=json \
    > operator-evidence/document-embedding-jobs.json
  python - <<'PY' > operator-evidence/document-embedding-jobs.txt
import json
from pathlib import Path
for job in json.loads(Path('operator-evidence/document-embedding-jobs.json').read_text()):
    name = (job.get('metadata', {}).get('name') or job.get('name', '')).rsplit('/', 1)[-1]
    if name in {'documind-ingest-batch', 'documind-reconcile'}:
        print(name)
PY
  while IFS= read -r job; do
    gcloud run jobs update "$job" --project="$PROJECT" --region="$REGION" --image="$RAG_REPAIR_IMAGE"
  done < operator-evidence/document-embedding-jobs.txt
  printf 'Ingest image: %s\n' "$RAG_REPAIR_IMAGE" \
    > operator-evidence/document-embedding-image.txt

  export GOOGLE_CLOUD_PROJECT="$PROJECT" PYTHONPATH="$DEMO_ROOT:$DEMO_ROOT/services/ingest"
  export MANAGED_MIRROR=off
  export VECTOR_INDEX_NAME="$(terraform -chdir=terraform output -raw vector_index_name)"
  test -n "$VECTOR_INDEX_NAME"
  unset VECTOR_DRY_RUN
  # Default repair scope is Acme. Rerun with RAG_REPAIR_TENANT=zeta or globex for those tenants.
  RAG_REPAIR_TENANT="${RAG_REPAIR_TENANT:-acme}"
  python services/ingest/reconcile.py --project "$PROJECT" --tenant "$RAG_REPAIR_TENANT" --backfill-vectors \
    | tee operator-evidence/document-embedding-preview.json
  python services/ingest/reconcile.py --project "$PROJECT" --tenant "$RAG_REPAIR_TENANT" --backfill-vectors --apply \
    | tee operator-evidence/document-embedding-applied.json
  python services/ingest/reconcile.py --project "$PROJECT" --tenant "$RAG_REPAIR_TENANT" --backfill-vectors \
    | tee operator-evidence/document-embedding-after.json
  python - <<'PY'
import json
from pathlib import Path
report = json.loads(Path('operator-evidence/document-embedding-after.json').read_text())
assert report['current_chunks'] > 0, 'STOP: no current chunks for this tenant'
assert report['invalid_chunks'] == 0, 'STOP: invalid canonical chunks'
assert report['needs_document_embedding'] == 0, 'STOP: unfinished embedding repair; rerun backfill'
print('PASS: current chunks have document embeddings. Prove live retrieval with the original question next.')
PY
)
```

The same backfill CLI can migrate Zeta and Globex without rebuilding the image again; pass `--tenant zeta`
or `--tenant globex` and the same project/index/model/version environment. Keep `EMBEDDING_MODEL` and
`EMBEDDING_VERSION` equal to the deployed API (the runbook uses `text-embedding-005` and `1`). A full model
or dimensionality change is a separate release, not this task-type correction.

After the final preview reports zero stale vectors, refresh the member token and repeat the original
probation question. ANN propagation can lag an accepted upsert; a global datapoint count cannot show that
an existing vector was replaced. Accept only an answer with `answerable=true`, a citation to the current
Acme HR policy supporting PB-02, six months, and `stages.vector_chunks > 0`. For SSE, `citation` events
identify the context sent to the generator, not proof that the generated answer is supported. Inspect the
answer as well. If performance-review passages still dominate after propagation, keep the gate failed and
inspect candidate IDs/ranker results; do not claim resolution by changing the question or increasing request
`top_k` (that changes the selected context size, not the initial 20-candidate pool).

Offline regression coverage includes document task type, malformed embedding responses, task-aware reuse,
tenant isolation, undo, adoption, failed upserts/commits, resume and concurrent-change preconditions:

```bash
python -m unittest discover -s commands/tests -p test_document_embeddings.py
```

These tests do not measure live Vertex retrieval quality.
