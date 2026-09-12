# evals/demo - documents the rehearsal drops into the lane on purpose

Not part of the corpus: `evals/upload.sh` walks `corpus/` only, so nothing here reaches the bucket by `make ingest-corpus`,
and the offline gate never sees these files. They exist for 12.5's ledger demo (`make reindex`, `make retire`, `make reconcile`):

| File | Shape | How it goes in | What the worker does |
|---|---|---|---|
| `hr_policy_2026_v2.md` | A. the same document re-issued (NP-03 notice period 60 -> 90 days, dated) | `make reindex FILE=evals/demo/hr_policy_2026_v2.md NAME=hr_policy_2026.md TENANT=acme` | indexes revision 2 as current, retires revision 1's chunks (`ingest_superseded`), drops ACME's cache record |
| `gratuity_amendment_2026.md` | B. a dated overlay on another document | `make ingest-one FILE=evals/demo/gratuity_amendment_2026.md TENANT=acme` | indexes it beside the Act; the model follows the later effective date and says so |

The undo is the original bytes again: `make reindex FILE=evals/corpus/acme/hr_policy_2026.md TENANT=acme` reactivates
revision 1 (nothing is re-embedded) and retires revision 2. Golden rows move with the policy in the same commit
(`lk-06` expects 60 days today) - the red gate in between is the demo.
