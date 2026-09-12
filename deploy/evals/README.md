# deploy/evals — the corpus and the golden set

Flagship Plan, Phase 0 (P0, due Mon 7 Sept): *"Synthetic corpus … golden.jsonl v0 (30 rows +
tenant-isolation). Every demo needs the same data; no real PII on screen."* Since 5 Sept the
corpus also holds **real documents** — thirteen of them since 6 Sept: twelve Acts and Codes of
Parliament and the ministry's compliance handbook for employers — and the golden set asserts on them.

```bash
python deploy/evals/fetch_real.py      # the real Acts: download from the publisher, verify sha256, extract mirrors
python deploy/evals/build_corpus.py    # corpus/ (synthetic documents) + manifest.json   (deterministic, no network)
python deploy/evals/build_golden.py    # golden.jsonl                                    (verified against corpus/)
bash   deploy/evals/upload.sh          # -> gs://$PROJECT_ID-uploads/<tenant>/
python deploy/evals/run_eval.py        # the offline gate (12.7): the golden set is sound
python deploy/evals/ablate.py --project P   # the ablation harness (4.8, Part 5): four retrieval arms, no model, ~3 min
python tools/check_real_corpus.py      # the retrieval gate: chunked the notebooks' way, the corpus answers it
```

The builders are deterministic: same input, same bytes. A diff means someone changed the corpus
on purpose, which is the point — when a `must_contain` is loosened to make a test pass, that is
a line change with an author and a date. `fetch_real.py` is the only step that touches the
network, and it refuses a file whose sha256 differs from `real_sources.json` unless told
`--allow-drift`.

## What is real and what is not

| | Documents | Why |
|---|---|---|
| **Real** | The four Labour Codes (Wages 2019; Social Security, Industrial Relations and Occupational Safety 2020), the Payment of Bonus Act 1965, the Payment of Gratuity Act 1972, the Maternity Benefit Act 1961 and its 2017 amendment (Gazette), the POSH Act 2013 (scanned Gazette), the DPDP Act 2023, the IT Act 2000, the CGST Act 2017 (236 pages), and the ministry's *Compliance Handbook for Employers Under the Four Labour Codes* | An Act of Parliament is real, Indian, exactly what an HR policy has to comply with, carries no personal data, and reproducing it is not an infringement (Copyright Act 1957, s. 52(1)(q)). A real company's handbook fails at least one of those tests. |
| **Synthetic** | The three tenants' handbooks, MSAs, the Hindi–English invoice, the annual report, the PAN/GSTIN/Aadhaar/mobile on the invoice | A corpus where every tenant holds the same law **cannot demonstrate tenant isolation**: the leaked answer would also be the correct one. The tenants' figures deliberately differ (below). And an invoice with a real PAN must never be on a shared screen. |

`real_sources.json` records, for each real document, the publisher's URL, the sha256 of the bytes
taken, the page count, whether it has a text layer, and which tenants hold it. `fetch_real.py`
writes `corpus/<tenant>/<slug>.pdf` — the object `upload.sh` pushes and the ingest worker and
lesson 4.1 parse with Document AI — and, for every text-layer PDF, `corpus/<tenant>/<slug>.md`:
the pypdf text of each page under a provenance header, pages separated by a form feed so page
numbers survive chunking. The mirror is what the offline gates and the notebooks' zero-cost seed
path (`shared/documind_corpus.py`) read; `upload.sh` leaves it home so nothing is indexed twice.
`posh_act_2013` is a scanned Gazette with no text layer and so has no mirror: it is the document
only the OCR lane can read, which is what lesson 4.1 uses it for.

The synthetic identifiers are **format-valid** so the DLP and Model Armor demos actually fire on
them, and **invented** so nothing real is ever on a shared screen.

| Field | Value | Why format-valid matters |
|---|---|---|
| PAN | `AAAPZ1234C` | 12.6's PII gate matches the 5-alpha/4-digit/1-alpha shape |
| GSTIN | `27AAAPZ1234C1ZV` | same, and it appears on the Hinglish invoice |
| Mobile | `+919876543210` | the `(?<!\d)` guard in 12.6 was written against exactly this |
| Aadhaar | `2234 5678 9012` | spaced form, which is how it appears on scanned documents |

## The corpus

| Tenant | Document | Type | Real? | Pages | ~Tokens |
|---|---|---|---|---|---|
| acme | `hr_policy_2026` | policy | synthetic | — | 40,096 |
| acme | `msa_acme_2026` | contract | synthetic | — | 232 |
| acme | `inv_2026_0412` | invoice | synthetic | — | 195 |
| acme | `annual_report_2026` | report | synthetic | — | 177 |
| acme | `annual_report_2026_fig3.png` | figure | drawn by `build_media.py` from the AR-02 table | 1 | caption at ingest |
| acme | `inv_2026_0412.png` | figure | the invoice rendered as a page image (`build_media.py`) | 1 | caption at ingest |
| acme | `payment_of_bonus_act_1965_p30.png` | figure | **real** page 30 of the Act below, rendered by pypdfium2 | 1 | caption at ingest |
| acme | `townhall_2026_q1.md` / `.mp4` | transcript / video | synthetic script, committed; the MP4 `build_media.py --video` synthesises from it (two Chirp 3 HD voices) | — | 425 / segments at ingest |
| acme | `code_on_wages_2019` | statute | **real** (Ministry of Labour & Employment) | 29 | 23,953 |
| acme | `payment_of_bonus_act_1965` | statute | **real** (Ministry of Labour & Employment) | 31 | 19,186 |
| acme | `payment_of_gratuity_act_1972` | statute | **real** (Ministry of Labour & Employment) | 10 | 8,930 |
| acme | `maternity_benefit_act_1961` | statute | **real** (Ministry of Labour & Employment) | 11 | 7,182 |
| acme | `maternity_benefit_amendment_act_2017` | statute | **real** (Ministry of Labour & Employment) | 4 | 1,928 |
| acme | `posh_act_2013` | statute | **real** (Gujarat Informatics Ltd) — scanned, no text layer | 13 | — |
| acme | `code_on_social_security_2020` | statute | **real** (Ministry of Labour & Employment) | 116 | 94,528 |
| acme | `industrial_relations_code_2020` | statute | **real** (Ministry of Labour & Employment) | 56 | 48,274 |
| acme | `osh_code_2020` | statute | **real** (Ministry of Labour & Employment) | 86 | 67,876 |
| acme | `labour_codes_compliance_handbook` | guidance | **real** (Ministry of Labour & Employment) | 38 | 12,560 |
| acme | `dpdp_act_2023` | statute | **real** (Ministry of Electronics and Information Technology) | 21 | 16,085 |
| acme | `it_act_2000` | statute | **real** (Ministry of Electronics and Information Technology) | 34 | 26,866 |
| acme | `cgst_act_2017` | statute | **real** (Central Board of Indirect Taxes and Customs) | 236 | 116,941 |
| zeta | `hr_policy_zeta_2026` | policy | synthetic | — | 40,096 |
| zeta | `msa_zeta_2026` | contract | synthetic | — | 232 |
| zeta | `code_on_wages_2019` | statute | **real** (Ministry of Labour & Employment) | 29 | 23,953 |
| zeta | `code_on_social_security_2020` | statute | **real** (Ministry of Labour & Employment) | 116 | 94,528 |
| zeta | `industrial_relations_code_2020` | statute | **real** (Ministry of Labour & Employment) | 56 | 48,274 |
| zeta | `osh_code_2020` | statute | **real** (Ministry of Labour & Employment) | 86 | 67,876 |
| zeta | `labour_codes_compliance_handbook` | guidance | **real** (Ministry of Labour & Employment) | 38 | 12,560 |
| globex | `msa_globex_2026` | contract | synthetic | — | 233 |
| globex | `dpdp_act_2023` | statute | **real** (Ministry of Electronics and Information Technology) | 21 | 16,085 |
| globex | `it_act_2000` | statute | **real** (Ministry of Electronics and Information Technology) | 34 | 26,866 |

**The tenants' synthetic figures deliberately differ.** ACME's travel cap is Rs 40,000, Zeta's is
Rs 25,000, Globex's is Rs 60,000; notice periods are 60 / 30 / 45 days. This is not decoration:
a corpus where every handbook says the same number cannot demonstrate tenant isolation.

**The real documents are deliberately unequal too.** ACME holds all thirteen; Zeta holds the four
Labour Codes and the handbook; Globex holds the DPDP and IT Acts. That is what makes the real
isolation rows real: *fifteen days' wages* and *twice the rate of wages* can only reach Globex from
another tenant, *the recommendations of the Council* (the CGST Act) and *a single point of contact*
(the DPDP Act) can only reach Zeta the same way — and it is what makes the twins honest: `lk-24`
and `lk-29` ask ACME and Zeta the same OSH question and both answer; `lk-14` and `lk-31` ask the
same maternity question and ACME answers from the 2017 Amendment while Zeta answers from the 2020
Code that consolidated it. Globex still has **no HR policy**, which is what makes `rf-03` and
`iso-03` real.

Chunked the way the notebooks chunk it (`shared/documind_corpus.py`: a handbook section per
chunk, fixed 2,000-character windows with page tracking for a PDF mirror), ACME is 1,624 chunks,
Zeta 1,000, Globex 126 — the volume at which a lexical top-5 starts to lose rows and dense retrieval
and the reranker (4.5) earn their place; `tools/check_real_corpus.py` and `tools/check_local_lane.py`
carry both measurements.

### Media is a document (Module 9)

`build_media.py` (`make media`) renders three PNGs into `corpus/acme/`, deterministic and committed:
Figure 3 of the annual report drawn from its own AR-02 table, the Hinglish invoice as a page image
(the English half of each label; the `.md` is the ground truth), and page 30 of the real Payment of
Bonus Act 1965 - the Fourth Schedule's set-on / set-off table. `--video` synthesises
`townhall_2026_q1.mp4` from the committed transcript `townhall_2026_q1.md` with two Chirp 3 HD
voices over four drawn slides (the Text-to-Speech API and ffmpeg - `apt-get install ffmpeg` in Cloud Shell, which
ships without it, or `pip install imageio-ffmpeg`); the MP4 and its
`.segments.json` ground truth are gitignored, and `upload.sh` keeps the transcript home when the
MP4 exists, the way it keeps a PDF's mirror home. The first slide says the video is synthetic.

`upload.sh` pushes them like any document. The ingest worker keys on the content type the
notification carries: an image is DESCRIBED by Gemini and indexed as one `figure` chunk (its
pixels DLP-scanned first, in Singapore: Mumbai offers no image inspection), a video or an MP3 becomes `segment` chunks with `start` / `end`, and
every consumer of `retrieve()` sees them as citations with `kind` and `media_url`. `manifest.json`
lists the rendered assets as `supplied_by: make media`; `acme/whiteboard_arch.png`, a photograph
of a hand-drawn architecture sketch, is still the owner's to supply. Check no real faces, voices
or colleagues are in anything you record.

## golden.jsonl — 64 rows

Lesson 4.7's schema, unchanged: `{id, shape, question, tenant, must_contain, must_retrieve,
answerable}`, `shape ∈ {lookup, join, refusal, isolation}`.

| Shape | Rows | What it tests |
|---|---|---|
| lookup | 34 | one chunk holds the answer — 18 of them over the real documents, asserting the document's own words (`twenty-six weeks`, `fifteen days`, `8.33`, `twice the normal rate`, `pro rata basis`, `single point of contact`) |
| join | 11 | two clauses, so packing order and budget matter — `jn-08` spans the amending Act and the principal Act it amends; `jn-10` the 1972 Gratuity Act's five years and the 2020 Code's pro-rata rule for fixed-term employees |
| refusal | 8 | the corpus does not contain it; saying so is the right answer — `rf-07` asks a GST rate the Act leaves to notifications; `mm-05` a Figure 7 the report never references |
| isolation | 11 | **release blockers** — a failure is a data leak, not a quality regression; `mm-04` asks Zeta about ACME's town hall |

The clause ids (`EXP-12`, `LV-01`, `PB-02`, `NP-03`, `LV-07`, `PR-05`, `IT-SEC-04`) and the
document slugs are the anchors `must_retrieve` names, and they are substrings of the chunk ids
`shared/documind_corpus.py` mints (`acme:hr_policy_2026#NP-03`,
`acme:maternity_benefit_amendment_act_2017#p2-0`), which is what 4.7's `recall_at_k` and 12.7's
`run_eval.py` match on. 4.7's inline rows are copied from this file, so the notebook scores a
subset of exactly what CI scores.

### Two schema extensions: `must_not_contain` and `must_cite_kind`

The five `mm-` rows (Module 9) carry `must_cite_kind`: `figure` or `segment`. Offline they are
verified like every other row, against the text the media was drawn from - Figure 3 from the
AR-02 table, page 30 from the Act's mirror, the town hall from its committed script. Live,
`run_eval.py` also reports how many of them came back with a citation of that kind, beside the
five thresholds and outside them: the figure is only there once `make media` and
`make ingest-corpus` have run, and a missing figure is a corpus state, not a model regression.

### `must_not_contain`

The isolation rows carry `must_not_contain`, which 4.7 does not have. Tenant isolation cannot be
expressed without it: *"the answer must not include another tenant's figure"* is the whole
assertion, and no combination of `must_contain` and `answerable` says that. `build_golden.py`
refuses a `must_not_contain` value that does not actually exist in some other tenant's corpus, or
that exists in the row's own, so every isolation row is a test that can genuinely fail.

## sft/ - the tuning dataset (Module 10)

`make trainset` builds it from the corpus mirrors: one question and one answer per real chunk, written by
gemini-3.6-flash in the lane's own grammar (the generator's SYSTEM, one `[Source 1]`, ModelDraft's JSON as
the target), every tenth chunk also a refusal, every row scanned by `shared/pii.py`, every generated
question checked for overlap with the golden set and dropped when it overlaps - the golden set is the test
set and never enters the file. Two formats from one list (`.vertex.jsonl` for managed SFT, `.chat.jsonl`
for unsloth and trl) and a manifest with the sha of each. Frozen: a changed corpus is a new version.
`run_eval.py` never reads this directory. Review the rows before committing them (4.7: a generated
question inherits the generator's blind spots).

## The comparison (Module 11)

`make compare` runs `services/slm/compare_backends.py` over the first twenty golden rows: retrieval once per
row through the one `retrieve()`, then every backend (`documind-general`, `documind-slm` by default) answers
from that same context through the gateway, so the only variable between rows is the model. Four numbers per
backend - groundedness over answerable rows, citation precision per cited chunk, p95 latency, rupees per
thousand queries - and the self-hosted rupees are a rate that only holds at the volume it was derived from
(11.4). `make judge API_B=<candidate>` is the other instrument: the same answers, Vertex AI Evaluation,
pairwise against the live revision.

## Refreshing the real documents

A publisher moves files. When `fetch_real.py` reports a 404, find the Act on the ministry's site
(labour.gov.in publishes central labour Acts under `static/uploads/`; India Code's direct links
answer a script with 403), put the new URL in `real_sources.json`, and run
`fetch_real.py --allow-drift`, which records the new sha256. Then `build_corpus.py`,
`build_golden.py` and the two gates: a changed consolidation can change a figure, and the golden
set has to be re-verified against the words the new file actually carries.
