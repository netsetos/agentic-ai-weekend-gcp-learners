#!/usr/bin/env python3
"""make cache: the first caller of 4.5's TenantCacheManager. Module 10 (10.2), hand-written in the kit.

    make cache PROJECT=... TENANT=acme                 # create: the tenant's synthetic documents as the cached pack
    make cache PROJECT=... TENANT=acme CACHE_OP=show   # show | refresh | delete
    cd services/rag-api && GOOGLE_CLOUD_PROJECT=P python cache_admin.py create --tenant acme

cache_manager.py shipped in the API image on 5 September and generator.py has read it on every request
since - and nothing ever CREATED a cache, so every answer paid full price for its context and
`tenant_caches` stayed empty. This is the create. The pack is the tenant's own synthetic documents
(the handbook, the MSA, the invoice, the report, the town hall transcript: the files under
evals/corpus/<tenant>/ with no PDF twin), which is over the 4,096-token minimum for the Gemini 3 family
and is exactly the material every question about the tenant's policies retrieves. The system
instruction is the generator's SYSTEM, so the cache and the request agree on the rules.

The cache is keyed to a MODEL (GENERATOR_MODEL) and to the corpus manifest's hash: generator.py only
attaches it when the model matches, and a changed corpus is a new cache. The first live create records
where it landed - global or regional - which is the question CLAUDE.md has carried since 4 September.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.dirname(os.path.dirname(HERE))
EVALS = os.path.join(DEPLOY, "evals")
# generator -> schemas -> shared.documind_schemas: in the image deploy/ is the working directory; from a
# shell it is two levels up, and the first `make cache` (F35, 10 September) stopped on "No module named
# 'shared'" before it reached the cache. The tool puts the tree on its own path, whatever the cwd.
sys.path.insert(0, DEPLOY)


def pack_for(tenant: str, corpus_dir: str = os.path.join(EVALS, "corpus")) -> tuple[str, str]:
    """The tenant's synthetic documents (no .pdf twin), joined with headers; and the corpus manifest's hash."""
    d = os.path.join(corpus_dir, tenant)
    parts = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".md") and not os.path.isfile(os.path.join(d, name[:-3] + ".pdf")):
            parts.append(f"### {name}\n" + open(os.path.join(d, name), encoding="utf-8").read().strip())
    manifest = os.path.join(EVALS, "manifest.json")
    version = hashlib.sha256(open(manifest, "rb").read()).hexdigest()[:12] if os.path.isfile(manifest) else "nomanifest"
    return "\n\n".join(parts), version


def selftest() -> int:
    """Offline: the pack is the synthetic documents only, and the generator imports from any cwd (F35)."""
    pack, version = pack_for("acme")
    assert len(pack) > 20_000 and "### hr_policy_2026.md" in pack, (len(pack), version)
    assert "### code_on_wages_2019.md" not in pack, "a document with a PDF twin is the real corpus, not the pack"
    assert len(version) == 12, version
    from shared.documind_schemas import ModelDraft  # noqa: F401 - the import that failed live: deploy/ on the path from any cwd
    try:
        from generator import SYSTEM
    except ImportError as e:                       # ModuleNotFoundError, or "cannot import name 'genai' from 'google'"
        missing = getattr(e, "name", None) or str(e)
        if "shared" in missing:
            raise AssertionError("deploy/ is not on the path: generator cannot import shared (F35)") from e
        print(f"selftest: the pack is {len(pack):,} chars of synthetic documents (corpus {version}); "
              f"the generator import was skipped here ({missing[:60]}) and runs where the kit is installed")
        return 0
    assert SYSTEM.startswith("You are DocuMind"), SYSTEM[:40]
    print(f"selftest: the pack is {len(pack):,} chars of synthetic documents (corpus {version}); "
          f"generator.SYSTEM imports with deploy/ on the path")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("op", nargs="?", choices=["create", "show", "refresh", "delete"])
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--ttl", type=int, default=int(os.environ.get("CACHE_TTL_S", "3600")))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.op:
        ap.error("an op is required: create | show | refresh | delete (or --selftest)")
    if not a.project:
        print("--project (or GOOGLE_CLOUD_PROJECT) is required", file=sys.stderr)
        return 2
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", a.project)
    from cache_manager import TenantCacheManager
    from generator import SYSTEM                       # the same rules the request carries
    mgr = TenantCacheManager(a.project)
    if a.op == "create":
        pack, version = pack_for(a.tenant)
        rec = mgr.create(a.tenant, SYSTEM, pack, ttl_s=a.ttl, version=version)
        print(f"  cache {rec['cache_name']}\n  location {rec['location']} (global or regional: the answer to CLAUDE.md's question)\n"
              f"  model {rec['model']} | tokens {rec['tokens']} | expires {rec['expire_time']} | corpus {version}")
        print(f"  the next /v1/query for {a.tenant} carries cached_content; read cached_tokens in its usage row")
        return 0
    rec = mgr.get(a.tenant, min_remaining_s=0)
    if a.op == "show":
        print(rec or f"  no cache for {a.tenant}")
        return 0
    if a.op == "refresh":
        print(mgr.refresh(a.tenant, ttl_s=a.ttl) or f"  no live cache for {a.tenant} to refresh")
        return 0
    mgr.delete(a.tenant)
    print(f"  deleted {a.tenant}'s cache" if rec else f"  nothing to delete for {a.tenant}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
