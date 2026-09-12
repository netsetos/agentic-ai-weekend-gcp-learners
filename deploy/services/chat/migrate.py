"""Create the checkpointer's tables - once. Lesson 8.5, gap G5.

`PostgresSaver.setup()` is idempotent, which makes it tempting to call at startup. Do not:
its migrations take ACCESS EXCLUSIVE locks and insert into a primary-keyed
checkpoint_migrations table, so N instances racing on a scale-out produce a deploy that
fails at random and succeeds on retry. So this is a JOB, run one time after `make up`
(commands/lesson-12.8.sh creates and executes it) and again only when the checkpointer
library is upgraded:

    CHECKPOINT_DSN='postgresql://chat:...@/documind?host=/cloudsql/PROJECT:REGION:INSTANCE' \\
        python migrate.py

The DSN is the same Secret Manager secret the service mounts; the job mounts it the same way.
"""
from __future__ import annotations

import os
import sys

from langgraph.checkpoint.postgres import PostgresSaver

dsn = os.environ.get("CHECKPOINT_DSN", "")
if not dsn or dsn == "memory":
    sys.exit("CHECKPOINT_DSN must be a Postgres DSN (terraform/cloudsql.tf writes it to Secret Manager)")

with PostgresSaver.from_conn_string(dsn) as saver:
    saver.setup()
print("checkpoint tables ready")
