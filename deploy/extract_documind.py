#!/usr/bin/env python3
"""
Extract the deployable DocuMind AI stack out of the Module 12 teaching
notebooks into deploy/ so it can be validated and deployed as real files.

The EIGHT Module-12 notebooks (12.1-12.8) store every deployable artifact as a
plain triple-quoted heredoc:

    CONFIG_PY = r'''
    ... file body ...
    '''
    with open('config.py', 'w') as f: f.write(CONFIG_PY)

Use the r prefix whenever the file contains a backslash. This decodes each
heredoc with ast.literal_eval, so a plain ''' would eat shell line-continuations
and \n escapes - silently, producing a file that is subtly not the one taught.

This reads those cells *statically* (regex only, NO code execution) and
materialises each file, routing by NAME first and then by extension:

    documind-cd.yml, documind-dryrun.yml -> .github/workflows/   (12.7)
    run-service.yaml                     -> deploy/              (12.7)
    run_eval.py                          -> deploy/evals/        (12.7)
    iap.py, tenancy.py                   -> deploy/shared/       (12.8)
    *.tf                                 -> deploy/terraform/
    *.sql                                -> deploy/terraform/sql/
    everything else                      -> deploy/services/<service>/
                                            (rag-api | admin | frontend | ingest | chat)

A file this table cannot place is REPORTED as UNPLACED, not skipped. Before that,
a heredoc with no destination was found, dropped, and counted as neither placed
nor unresolved - so `--check` stayed green over a notebook that wrote nothing.

gcloud / terraform command heredocs (DEPLOY, APPLY, ENABLE_APIS, SMOKE) are
collected into deploy/commands/<lesson>.sh as reference for the Makefile and
the live-day runbook -- they are NEVER executed by this script.

The notebooks are the single source of truth; this tree is regenerable.

USAGE
    python deploy/extract_documind.py            # extract (idempotent)
    python deploy/extract_documind.py --check    # exit 1 if the tree is stale
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root
DEPLOY = Path(__file__).resolve().parent               # deploy/

# lesson -> (notebook path relative to repo root, service dir for non-tf/sql files)
NOTEBOOKS = {
    # Module 7 owns one service: the MCP server is authored in 7.1 and deployed from 7.2, the
    # way 12.2 authors rag-api. Everything else the kit ships still comes from Module 12.
    "7.1": ("Module 7/gcp-capstone-lesson-7.1-complete/lesson-7.1/GCP_Capstone_7.1_FastMCP.ipynb", "services/mcp"),
    "7.2": ("Module 7/gcp-capstone-lesson-7.2-complete/lesson-7.2/GCP_Capstone_7.2_CloudRunDeploy.ipynb", "services/mcp"),
    "8.4": ("Module 8/gcp-capstone-lesson-8.4-complete/lesson-8.4/GCP_Capstone_8.4_A2A.ipynb", "services/agent"),
    "12.1": ("Module 12/gcp-capstone-lesson-12.1-complete/lesson-12.1/GCP_Capstone_12.1_InfraSetup.ipynb", None),
    "12.2": ("Module 12/gcp-capstone-lesson-12.2-complete/lesson-12.2/GCP_Capstone_12.2_RAGBackend.ipynb", "services/rag-api"),
    "12.3": ("Module 12/gcp-capstone-lesson-12.3-complete/lesson-12.3/GCP_Capstone_12.3_AdminObservability.ipynb", "services/admin"),
    "12.4": ("Module 12/gcp-capstone-lesson-12.4-complete/lesson-12.4/GCP_Capstone_12.4_StreamlitFrontend.ipynb", "services/frontend"),
    "12.5": ("Module 12/gcp-capstone-lesson-12.5-complete/lesson-12.5/GCP_Capstone_12.5_Ingestion.ipynb", "services/ingest"),
    # 12.6 ADDS modules to rag-api (guard, cost, semantic_cache, telemetry, breakers). It writes
    # no file that 12.2 owns, so the two notebooks never fight over the same destination.
    "12.6": ("Module 12/gcp-capstone-lesson-12.6-complete/lesson-12.6/GCP_Capstone_12.6_GuardObserve.ipynb", "services/rag-api"),
    # 12.7 is INFRA-shaped (service=None): it writes .tf, a GitHub Actions workflow and
    # root-level files. Those last two only have destinations because dest_for grew the
    # branches below - without them the heredocs are found and then dropped.
    "12.7": ("Module 12/gcp-capstone-lesson-12.7-complete/lesson-12.7/GCP_Capstone_12.7_KeylessCICD.ipynb", None),
    # 12.8 owns services/chat/ (adopted - it was in no lesson at all) plus the two shared
    # modules it introduces. shared/ is routed by SHARED_FILES below, not by the service dir.
    "12.8": ("Module 12/gcp-capstone-lesson-12.8-complete/lesson-12.8/GCP_Capstone_12.8_IntegrateSurfaces.ipynb", "services/chat"),
}

# VAR = '''...'''  or  VAR = """..."""   (heredoc, optionally r/f/b prefixed).
# Captures name, string prefix, quote, body — so the literal can be decoded with
# ast.literal_eval (which correctly handles \-line-continuations and escapes,
# reproducing exactly the file the notebook itself would write).
VAR_RE = re.compile(r"^([A-Z][A-Z0-9_]+)\s*=\s*((?:r|f|rf|fr|b)?)('''|\"\"\")(.*?)\3", re.S | re.M)


def decode_literal(prefix: str, quote: str, body: str) -> str:
    if "f" in prefix.lower():
        return body                       # f-string: keep raw (not used by our files)
    try:
        return ast.literal_eval(prefix + quote + body + quote)
    except Exception:
        return body
# with open('file','w') as f: f.write(VAR)  — same-line OR multi-line
WRITE_OPEN_RE = re.compile(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"]w['"][^)]*\)[\s\S]{0,120}?\.write(?:_text)?\(\s*([A-Za-z_]\w*)""")
# Path('file').write_text(VAR)
WRITE_PATH_RE = re.compile(r"""Path\(\s*['"]([^'"]+)['"]\s*\)\.write_text\(\s*([A-Za-z_]\w*)""")
# for name, content in [('network.tf', NETWORK_TF), ...]  — tuple-list loop writes
TUPLE_PAIR_RE = re.compile(r"""\(\s*['"]([^'"/][^'"]*\.[A-Za-z0-9_]+)['"]\s*,\s*([A-Z][A-Z0-9_]+)\s*\)""")
# %%writefile file
WRITEFILE_RE = re.compile(r"%%writefile\s+(\S+)")

# command-style heredoc vars we keep as *reference scripts*, not files.
# NOTE: a tuple, not a set — set iteration order is randomised per process
# (PYTHONHASHSEED), which would make the commands/*.sh section order flip
# between runs and break the `--check` freshness comparison.
COMMAND_VARS = ("ENABLE_APIS", "APPLY", "DEPLOY", "SMOKE")


def cell_src(cell) -> str:
    return "".join(cell.get("source", []))


def collect(nb_path: Path):
    """Return (files: {relpath: content}, commands: [(var, body)])."""
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    consts: dict[str, str] = {}
    file_map: list[tuple[str, str]] = []   # (filename, varname), in order
    commands: list[tuple[str, str]] = []
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        src = cell_src(cell)
        for name, prefix, quote, body in VAR_RE.findall(src):
            consts[name] = decode_literal(prefix, quote, body).lstrip("\n").rstrip() + "\n"
        for rx in (WRITE_OPEN_RE, WRITE_PATH_RE, TUPLE_PAIR_RE):
            for fname, var in rx.findall(src):
                file_map.append((fname, var))
        for fname in WRITEFILE_RE.findall(src):
            # writefile writes the *rest of the cell*, not a var; capture body after the magic line
            body = src.split("\n", 1)[1] if "\n" in src else ""
            consts.setdefault("__WF__" + fname, body.rstrip() + "\n")
            file_map.append((fname, "__WF__" + fname))

    files: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for fname, var in file_map:
        if var in consts:
            files[fname] = consts[var]
        else:
            missing.append((fname, var))
    for var in COMMAND_VARS:
        if var in consts:
            commands.append((var, consts[var]))
    return files, commands, missing


# Files that belong at the REPO ROOT, not under deploy/. Returned with a ".." prefix so
# (DEPLOY / rel) resolves them without write_tree or check_tree needing to know.
WORKFLOW_FILES = {"documind-cd.yml", "documind-dryrun.yml"}
ROOT_FILES: set[str] = set()          # e.g. a root skaffold.yaml, if one is ever written
# deploy/evals/ is otherwise hand-written (see evals/README.md). The RUNNER is the one
# file a lesson owns, because 12.7 teaches the matching rule it implements.
EVAL_FILES = {"run_eval.py"}
# Files that belong at the top of deploy/ itself.
DEPLOY_ROOT_FILES = {"run-service.yaml", "cloudbuild.yaml"}
# shared/ is imported by chat, ingest, admin AND rag-api at runtime. 12.8 owns iap/tenancy;
# 12.2 owns the answer contract (documind_schemas.py - gap G1); the older three
# (documind_tools, pii, audit_log) remain hand-written.
SHARED_FILES = {"iap.py", "tenancy.py", "documind_schemas.py"}


def dest_for(fname: str, service: str | None) -> Path | None:
    if fname in WORKFLOW_FILES:
        return Path("..") / ".github" / "workflows" / fname
    if fname in ROOT_FILES:
        return Path("..") / fname
    if fname in EVAL_FILES:
        return Path("evals") / fname
    if fname in DEPLOY_ROOT_FILES:
        return Path(fname)
    if fname in SHARED_FILES:
        return Path("shared") / fname
    if fname.endswith(".tf"):
        return Path("terraform") / fname
    if fname.endswith(".sql"):
        return Path("terraform") / "sql" / fname
    if service is None:
        # NOT a silent skip. A notebook that wrote this file meant it to land
        # somewhere; if this table has no destination, that is a bug HERE, and
        # build_tree reports it rather than dropping the file.
        return None
    return Path(service) / fname


def build_tree() -> dict[Path, str]:
    """Return {deploy-relative path: content} for the whole extracted tree."""
    tree: dict[Path, str] = {}
    report: list[str] = []
    for lesson, (rel, service) in NOTEBOOKS.items():
        nb_path = ROOT / rel
        if not nb_path.exists():
            report.append(f"  ! {lesson}: notebook missing ({rel})")
            continue
        files, commands, missing = collect(nb_path)
        placed = 0
        unplaced: list[str] = []
        for fname, content in files.items():
            d = dest_for(fname, service)
            if d is None:
                unplaced.append(fname)
                continue
            tree[d] = content
            placed += 1
        if commands:
            script = f"# Reference commands extracted from {lesson} ({Path(rel).name}).\n"
            script += "# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand\n"
            script += "# or via the Makefile live-tier targets. See deploy/README.md.\n\n"
            for var, body in commands:
                script += f"# ---- {var} ----\n{body.strip()}\n\n"
            tree[Path("commands") / f"lesson-{lesson}.sh"] = script
        report.append(f"  * {lesson}: {placed} file(s), {len(commands)} command block(s)"
                      + (f", {len(missing)} unresolved write(s)" if missing else "")
                      + (f", {len(unplaced)} UNPLACED" if unplaced else ""))
        for fname, var in missing:
            report.append(f"      unresolved: {fname} <- {var} (var not found as a heredoc)")
        for fname in unplaced:
            report.append(f"      UNPLACED: {fname} - dest_for() has no destination for it. "
                          f"Add it to WORKFLOW_FILES / ROOT_FILES or give the lesson a "
                          f"service dir; it was NOT written.")
    return tree, report


def write_tree(tree: dict[Path, str]) -> int:
    written = 0
    for rel, content in tree.items():
        out = DEPLOY / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        prev = out.read_text(encoding="utf-8") if out.exists() else None
        if prev != content:
            out.write_text(content, encoding="utf-8", newline="\n")
            written += 1
    return written


def check_tree(tree: dict[Path, str]) -> list[Path]:
    """Return list of stale/missing paths (committed tree != freshly extracted)."""
    stale = []
    for rel, content in tree.items():
        out = DEPLOY / rel
        if (not out.exists()) or out.read_text(encoding="utf-8") != content:
            stale.append(rel)
    return stale


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if deploy/ is out of date vs the notebooks")
    args = ap.parse_args()

    if not any((ROOT / rel).exists() for rel, _ in NOTEBOOKS.values()):
        # The learner repo carries this tree without the curriculum notebooks it was extracted from.
        print("kit-only checkout: no curriculum notebooks under Module */ - nothing to extract or check; deploy/ here IS the extracted tree")
        return 0
    tree, report = build_tree()
    print("DocuMind extraction — from Module 12 notebooks:")
    for line in report:
        print(line)

    if args.check:
        stale = check_tree(tree)
        if stale:
            print(f"\nSTALE ({len(stale)}): run `python deploy/extract_documind.py`:")
            for s in stale:
                print(f"  {s.as_posix()}")
            return 1
        print(f"\nUp to date — {len(tree)} files match the notebooks.")
        return 0

    n = write_tree(tree)
    print(f"\nWrote/updated {n} of {len(tree)} files under deploy/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
