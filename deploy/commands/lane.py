#!/usr/bin/env python3
"""The lane's records as direct Python calls (22 September 2026): what the mk/*.mk targets run, without make.

    python commands/lane.py sources [--tenant acme] [--json]                 make sources
    python commands/lane.py queued [--limit N]                              make queued
    python commands/lane.py vector-status                                   make vector-status
    python commands/lane.py backfill-vectors [--tenant acme] [--apply]      make backfill-vectors (the plan; --apply repairs)
    python commands/lane.py roster --tenant acme --members a@x.com,b@y.com [--dry-run]   make roster
    python commands/lane.py tenant-backend acme [vector|firestore|rag_engine|vertex_search|default]   make tenant-backend
    python commands/lane.py tenant-policy acme [in|any]                     make tenant-policy

The project is --project, else PROJECT, else GOOGLE_CLOUD_PROJECT; the region --region, else REGION, else us-central1
(where the index lives: vector.tf's var.region). Each subcommand is the kit's own module - services/ingest/reconcile.py,
batch.py, shared/tenancy.py - called the way its make target calls it, so the two never disagree. The index and
endpoint names come from the shell (VECTOR_INDEX_NAME, VECTOR_INDEX_ENDPOINT), else the one index and endpoint
vector.tf names (documind-chunks, documind-endpoint), else Terraform's outputs. The drills that upload and wait
(ingest-one, poison, reindex, wait-vectors) are shell by nature: commands/*.sh, run the same way without make.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # the kit: deploy/ in the learner repo
INGEST = ROOT / "services" / "ingest"
GOLDEN_TENANTS = ("acme", "zeta", "globex")
INDEX_DISPLAY_NAME, ENDPOINT_DISPLAY_NAME = "documind-chunks", "documind-endpoint"   # vector.tf


def _prepare(project: str) -> None:
    """The environment the worker's modules read, and the import paths the image lays out."""
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    os.environ.setdefault("PROJECT", project)
    for p in (str(ROOT), str(INGEST)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _terraform_output(name: str) -> str:
    try:
        r = subprocess.run(["terraform", f"-chdir={ROOT / 'terraform'}", "output", "-raw", name],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _by_display_name(kind: str, display_name: str, project: str, region: str) -> str:
    """The resource name of the one index (or endpoint) vector.tf declares, through the SDK; empty when none or no access."""
    try:
        from google.cloud import aiplatform
        aiplatform.init(project=project, location=region)
        cls = aiplatform.MatchingEngineIndex if kind == "index" else aiplatform.MatchingEngineIndexEndpoint
        found = cls.list(filter=f'display_name="{display_name}"')
        return found[0].resource_name if found else ""
    except Exception as e:  # noqa: BLE001 - the SDK, the API or the permission: the caller falls back to Terraform
        print(f"({kind}: aiplatform could not list by name, {type(e).__name__}; trying terraform output)", file=sys.stderr)
        return ""


def index_name(project: str, region: str) -> str:
    return (os.environ.get("VECTOR_INDEX_NAME") or _by_display_name("index", INDEX_DISPLAY_NAME, project, region)
            or _terraform_output("vector_index_name"))


def endpoint_name(project: str, region: str) -> str:
    return (os.environ.get("VECTOR_INDEX_ENDPOINT") or _by_display_name("endpoint", ENDPOINT_DISPLAY_NAME, project, region)
            or _terraform_output("vector_index_endpoint"))


def _reconcile(argv: list[str]) -> int:
    """services/ingest/reconcile.py, called as make calls it (it parses sys.argv)."""
    import reconcile
    sys.argv = ["reconcile.py"] + argv
    return int(reconcile.main() or 0)


# ---------------------------------------------------------------- the subcommands
def cmd_sources(a) -> int:
    _prepare(a.project)
    argv = ["--project", a.project, "--report"]
    if a.tenant:
        argv += ["--tenant", a.tenant]
    if a.json:
        argv.append("--json")
    return _reconcile(argv)


def cmd_queued(a) -> int:
    _prepare(a.project)
    import batch
    sys.argv = ["batch.py", "--project", a.project, "--queued"] + (["--limit", str(a.limit)] if a.limit else [])
    return int(batch.main() or 0)


def cmd_vector_status(a) -> int:
    from google.cloud import aiplatform
    aiplatform.init(project=a.project, location=a.region)
    idx = index_name(a.project, a.region)
    if not idx:
        print(">> no index: VECTOR_INDEX_NAME is empty, no index named documind-chunks, no terraform output - vector.tf is not applied")
        return 0
    d = aiplatform.MatchingEngineIndex(idx).to_dict()
    stats = d.get("indexStats") or {}
    print(f"index: {d.get('displayName')}  datapoints: {stats.get('vectorsCount', '0')}  shards: {stats.get('shardsCount', '0')}  update: {d.get('indexUpdateMethod')}")
    ep = endpoint_name(a.project, a.region)
    if not ep:
        print(">> no endpoint name: the deployment was not read")
        return 0
    e = aiplatform.MatchingEngineIndexEndpoint(ep).to_dict()
    for di in e.get("deployedIndexes") or []:
        print(f"endpoint: {e.get('displayName')}  deployed: {di.get('id')}  synced: {di.get('indexSyncTime', '-')}")
    return 0


def cmd_backfill_vectors(a) -> int:
    _prepare(a.project)
    idx = index_name(a.project, a.region)
    if not idx:
        print("VECTOR_INDEX_NAME is empty: no index to fill (vector.tf is not applied)")
        return 2
    os.environ["VECTOR_INDEX_NAME"] = idx
    argv = ["--project", a.project, "--backfill-vectors"]
    if a.tenant:
        argv += ["--tenant", a.tenant]
    if a.apply:
        argv.append("--apply")
    return _reconcile(argv)


def roster_plan(project: str, tenant: str, members: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """What make roster does, as data: (tenant, email) memberships and (tenant, data_region) policies.

    The members go on TENANT's roster. The UI's service account is the bearer identity of make smoke and make eval-live
    (shared/iap.py's other leg; 12.2's SMOKE note), so it sits on the three golden tenants; the MCP server's account (7.2)
    retrieves for the agent that called it and the API checks ITS email on the roster like any other caller; the chat
    service's account (12.8) is a surface like the UI's; the A2A peer's account (8.4) sits on ONE roster, acme. The
    demo's data-region policies (13 September 2026, evening): acme and zeta may be mirrored into the managed stores
    and read from them; globex's text never leaves the kit's own rows."""
    sa = lambda name: f"documind-{name}-sa@{project}.iam.gserviceaccount.com"  # noqa: E731
    plan = [(tenant, m) for m in members]
    for name in ("ui", "mcp", "chat"):
        plan += [(t, sa(name)) for t in GOLDEN_TENANTS]
    plan.append(("acme", sa("agent")))
    policies = [("acme", "any"), ("zeta", "any"), ("globex", "in")]
    return plan, policies


def cmd_roster(a) -> int:
    members = [m for m in a.members.replace(",", " ").split() if m]
    plan, policies = roster_plan(a.project, a.tenant, members)
    if a.dry_run:
        for t, e in plan:
            print(f"would put {e.lower()} on {t}")
        for t, r in policies:
            print(f"would set {t}: data_region={r}")
        return 0
    _prepare(a.project)
    from shared import tenancy
    for t, e in plan:
        tenancy.add_member(t, e)
        print(f"{e.lower()} is on {t}")
    for t, r in policies:
        print(f"{t}: data_region={tenancy.set_policy(t, r)}")
    return 0


def cmd_tenant_backend(a) -> int:
    _prepare(a.project)
    from shared import tenancy
    if a.backend:
        print(f"{a.tenant}: retrieval_backend={tenancy.set_backend(a.tenant, a.backend)}")
    else:
        print(f"{a.tenant}: retrieval_backend={tenancy.backend_for(a.tenant) or 'default (the deployment RETRIEVAL_BACKEND)'}")
    return 0


def cmd_tenant_policy(a) -> int:
    _prepare(a.project)
    from shared import tenancy
    if a.region_policy:
        print(f"{a.tenant}: data_region={tenancy.set_policy(a.tenant, a.region_policy)}")
    else:
        print(f"{a.tenant}: data_region={tenancy.policy_for(a.tenant)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[2:]))
    ap.add_argument("--project", default=os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "")
    ap.add_argument("--region", default=os.environ.get("REGION") or "us-central1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sources", help="the versions view: every source's current version and the corpus fingerprint (make sources)")
    s.add_argument("--tenant"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_sources)
    q = sub.add_parser("queued", help="the batch lane's queue (make queued)")
    q.add_argument("--limit", type=int); q.set_defaults(fn=cmd_queued)
    v = sub.add_parser("vector-status", help="the index and its deployment (make vector-status)")
    v.set_defaults(fn=cmd_vector_status)
    b = sub.add_parser("backfill-vectors", help="the tier from the rows: the plan, or --apply to repair (make backfill-vectors)")
    b.add_argument("--tenant"); b.add_argument("--apply", action="store_true"); b.set_defaults(fn=cmd_backfill_vectors)
    r = sub.add_parser("roster", help="the members on a tenant's roster, the service accounts on the golden tenants, the data-region policies (make roster)")
    r.add_argument("--tenant", default="acme"); r.add_argument("--members", default="")
    r.add_argument("--dry-run", action="store_true", help="print the plan; write nothing"); r.set_defaults(fn=cmd_roster)
    tb = sub.add_parser("tenant-backend", help="which store answers a tenant; alone, print it (make tenant-backend)")
    tb.add_argument("tenant"); tb.add_argument("backend", nargs="?"); tb.set_defaults(fn=cmd_tenant_backend)
    tp = sub.add_parser("tenant-policy", help="where a tenant's text may be held: in | any; alone, print it (make tenant-policy)")
    tp.add_argument("tenant"); tp.add_argument("region_policy", nargs="?", choices=("in", "any")); tp.set_defaults(fn=cmd_tenant_policy)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.project and not (a.cmd == "roster" and a.dry_run):
        ap.error("--project is required (or PROJECT in the environment)")
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
