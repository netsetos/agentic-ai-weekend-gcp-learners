# mk/ - the Makefile, one file per lane

The kit's `Makefile` keeps what every target shares: the variables (`PROJECT`, `REGION`, the Terraform variable
list, the service list), `guard-project`, `.PHONY`, and the core lifecycle of a lane (`plan`, `up`, `build`,
`deploy-services`, `down`, the smokes, the release). Everything a course module teaches as its own operations lives
here, one file per lane, pulled in by `include mk/*.mk`:

| File | Targets | Course module |
|---|---|---|
| `ingestion.mk` | `roster`, `tenant-policy`, `tenant-backend`, `vector-status`, `wait-vectors`, `backfill-vectors`, `ingest-one`, `poison`, `dlq` | 3 Ingestion |
| `lifecycle.mk` | `reindex`, `retire`, `restore`, `reconcile`, `backfill-current`, `reconcile-job`, `sources`, `purge`, `smoke-reindex`, `batch-job`, `batch`, `queued` | 4 Lifecycle |

Three rules keep the files small:

1. **A target is a one-line entry.** A recipe longer than a few lines is a script under `commands/` (`ingest-one.sh`,
   `poison.sh`, `reindex.sh`, `wait-vectors.sh`, `vector-status.sh`) or a subcommand of `commands/lane.py`
   (`sources`, `queued`, `vector-status`, `backfill-vectors`, `roster`, `tenant-backend`, `tenant-policy`). The
   make target and the direct call run the same code, so a learner without `make` - Windows, Colab - runs
   `PROJECT=<id> TENANT=acme FILE=... bash commands/ingest-one.sh` or `python commands/lane.py sources`.
2. **The comment travels with the target.** The design notes that explained a target in the Makefile sit above it
   here, unchanged.
3. **A new module is a new file.** Add `mk/<lane>.mk`, list its targets in the Makefile's `.PHONY`, and give any
   long recipe a script or a `lane.py` subcommand first. Nothing else changes: `tools/check_*.py` read the
   Makefile and `mk/*.mk` as one text, and the learner mirror (`tools/build_learners.py`) copies every tracked
   file under the kit.

Moved on 22 September 2026, with every target's name, comment and recipe kept byte for byte except the six that
became scripts and one comment that still said five delivery attempts (eventarc.tf says twelve); the scripts also fix a latent slip in the old recipes, where a `FILE=` or golden-set check that
failed inside `( ...; exit 1 )` did not stop the recipe.
