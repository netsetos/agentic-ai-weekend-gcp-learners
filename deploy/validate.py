#!/usr/bin/env python3
"""
DocuMind AI — Tier-A offline dry run.

Validates the deployable stack extracted from the Module 12 notebooks WITHOUT
touching GCP and WITHOUT spending money. This is the "does it even hold
together" gate you run on every push and before a live session.

Checks (each -> PASS / WARN / FAIL / SKIP):
  1. extract      deploy/ tree is in sync with the notebooks
  2. py_compile   every service .py parses (syntax)
  3. imports      no unresolved local imports (missing sibling modules)
  3b. shared-deps a google.cloud client that a shared/ module opens - even lazily, inside a
                  function - is pinned by every service that imports that module
  4. requirements every dependency is version-pinned
  5. pins         a package two services share is pinned to the same version in both
  6. dockerfile   every service ships a Dockerfile
  7. copy-paths   every COPY source exists in the context its Dockerfile assumes (no Docker needed)
  8. terraform    `terraform fmt -check` + `init -backend=false` + `validate`
  9. tflint       terraform lint
 10. docker       `docker build` each python-based image from its own context (no push);
                  Dockerfiles on vendor bases (vLLM, LiteLLM, Ollama) are parsed and linted

terraform / tflint / docker are SKIPPED (not failed) when the tool is not on
PATH -- CI (Linux) runs them; locally you still get 1-7. Exit code is non-zero
only if something FAILED (WARN / SKIP never fail the run). On GitHub Actions every
WARN / FAIL is also an annotation and the table is the job summary, so a red run
reads from the commit page - the raw job log is admin-only.

USAGE
    python deploy/validate.py            # run all applicable checks
    python deploy/validate.py --strict   # treat WARN as failure too
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parent
SERVICES = DEPLOY / "services"
TERRAFORM = DEPLOY / "terraform"

STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "typing", "dataclasses", "functools", "datetime", "uuid", "json", "os",
    "sys", "time", "logging", "re", "pathlib", "asyncio", "collections", "io",
    "hashlib", "base64", "enum", "contextlib", "math", "urllib",
}
# import-name (top segment) -> known third-party, so a missing sibling module
# is not confused with a pip dependency.
THIRD_PARTY = {
    "fastapi", "starlette", "pydantic", "pydantic_settings", "uvicorn",
    "gunicorn", "streamlit", "pandas", "plotly", "numpy", "google", "grpc",
    "opentelemetry", "requests", "httpx", "tenacity", "litellm", "vertexai",
    "PIL", "reportlab", "openai", "vllm", "anyio", "dotenv", "jwt", "jose",
    "altair", "pyarrow", "tiktoken", "sqlalchemy", "pg8000",
    # lesson 6.4's chat service runs the LangChain tool loop
    "langchain", "langchain_core", "langchain_google_genai", "langgraph",
    "langchain_chroma", "chromadb",
}

C = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}
results: list[tuple[str, str, str]] = []   # (check, status, detail)
raw: dict[str, str] = {}                   # check -> the tool's own output, for the CI job summary


def add(check: str, status: str, detail: str = "", output: str = "") -> None:
    results.append((check, status, detail))
    if output:
        raw[check] = output


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


# ---- 1. extraction freshness ------------------------------------------------
def check_extract() -> None:
    code, out = run([sys.executable, str(DEPLOY / "extract_documind.py"), "--check"], ROOT)
    if "kit-only checkout" in out:
        add("extract", "SKIP", "kit-only checkout (the learner repo): deploy/ is the extracted tree, nothing to compare")
    elif code == 0:
        add("extract", "PASS", "deploy/ matches the Module 12 notebooks")
    else:
        n = out.count("\n  ")
        add("extract", "FAIL", "deploy/ is stale — run `python deploy/extract_documind.py`")


# ---- 2. python syntax -------------------------------------------------------
def service_pys() -> list[Path]:
    return sorted(SERVICES.rglob("*.py"))


def check_pycompile() -> None:
    # shared/ and evals/ are NOT under services/, so rglob missed them - and shared/ is the
    # one directory three services import at runtime (chat, ingest, admin all build from the
    # deploy/ context precisely so it ships in the image). A syntax error in shared/iap.py
    # would have taken all three down with `make dryrun` still green.
    pys = (service_pys()
           + sorted((DEPLOY / "smoke").glob("*.py"))
           + sorted((DEPLOY / "shared").glob("*.py"))
           + sorted((DEPLOY / "evals").glob("*.py")))
    bad = []
    for py in pys:
        try:
            ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            bad.append(f"{py.relative_to(DEPLOY).as_posix()}:{e.lineno} {e.msg}")
    if not bad:
        add("py_compile", "PASS", f"{len(pys)} python files parse")
    else:
        add("py_compile", "FAIL", "; ".join(bad))


# ---- 3. unresolved local imports -------------------------------------------
def top_imports(tree: ast.AST):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.level and n.level > 0:          # relative: from . import x / from .x
                yield (n.module or "").split(".")[0] or (n.names[0].name if n.names else ""), True
            elif n.module:
                yield n.module.split(".")[0], False
        elif isinstance(n, ast.Import):
            for a in n.names:
                yield a.name.split(".")[0], False


def check_imports() -> None:
    unresolved = []
    # deploy/shared/ is copied into every service image beside the service's own files (see the
    # Dockerfiles), so `from shared import ...` resolves at runtime even though it is not a
    # sibling .py. Lesson 8.7 is why the import exists: one retrieval implementation, shared.
    shared_pkgs = {"shared"} if (DEPLOY / "shared").is_dir() else set()
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        # A service's OWN requirements.txt is the authority on what it may import. Hard-coding
        # every module name in THIRD_PARTY means a dependency that is correctly declared and
        # correctly installed still fails this check - which is how `streamlit_mic_recorder`
        # (declared as streamlit-mic-recorder) showed up as unresolved.
        declared = set()
        req = svc / "requirements.txt"
        if req.is_file():
            for ln in req.read_text(encoding="utf-8").splitlines():
                ln = ln.split("#", 1)[0].strip()
                if not ln:
                    continue
                name = re.split(r"[=<>!\[;]", ln, maxsplit=1)[0].strip()
                if name:
                    declared.add(name.replace("-", "_").lower())
        stems = {p.stem for p in svc.glob("*.py")} | shared_pkgs | declared
        for py in svc.glob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for mod, relative in top_imports(tree):
                if not mod:
                    continue
                if mod in stems:
                    continue                      # resolved local sibling
                if relative:
                    unresolved.append(f"{svc.name}/{py.name}: from .{mod} (no {mod}.py)")
                    continue
                if mod in STDLIB or mod in THIRD_PARTY:
                    continue
                if mod.islower() and "_" not in mod[:1]:
                    # lowercase bare import, not stdlib/third-party, no sibling -> likely missing local module
                    unresolved.append(f"{svc.name}/{py.name}: `{mod}` (no {mod}.py, not a known dep)")
    if not unresolved:
        add("imports", "PASS", "all local imports resolve")
    else:
        uniq = sorted(set(unresolved))
        add("imports", "FAIL", f"{len(uniq)} unresolved: " + " | ".join(uniq))


# ---- 3b. shared deps: a cloud client a shared module opens is pinned by every importer -----
# The chat service's first live smoke (2026-09-09) answered 500 to every /v1/chat, the outsider
# included: shared/tenancy.py opens Firestore lazily, inside tenant_for(), and the chat image had
# never installed the client. Check 3 cannot see that - `shared` resolves, and the import sits
# inside a function body. This check follows each service's `from shared import ...` (any depth,
# transitively through shared/) and demands that every google.cloud.<pkg> those modules touch is
# declared in the service's own requirements.txt.
CLOUD_DIST = {"dlp_v2": "dlp", "pubsub_v1": "pubsub", "documentai_v1": "documentai",
              "secretmanager": "secret-manager", "secretmanager_v1": "secret-manager",
              "aiplatform_v1": "aiplatform", "bigquery_storage": "bigquery-storage"}


def _cloud_clients(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            if n.module == "google.cloud":
                out |= {a.name.split(".")[0] for a in n.names}
            elif n.module.startswith("google.cloud."):
                out.add(n.module.split(".")[2])
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("google.cloud."):
                    out.add(a.name.split(".")[2])
    return out


def _shared_imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            if n.module == "shared":
                out |= {a.name for a in n.names}
            elif n.module.startswith("shared."):
                out.add(n.module.split(".")[1])
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("shared."):
                    out.add(a.name.split(".")[1])
    return out


def check_shared_deps() -> None:
    shared_dir = DEPLOY / "shared"
    if not shared_dir.is_dir():
        add("shared-deps", "SKIP", "no deploy/shared")
        return
    trees: dict[str, ast.AST] = {}
    for py in shared_dir.glob("*.py"):
        try:
            trees[py.stem] = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
    missing = []
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        req = svc / "requirements.txt"
        if not req.is_file():
            continue
        declared = set()
        for ln in req.read_text(encoding="utf-8").splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                name = re.split("[=<>!;]", ln.split("[", 1)[0], maxsplit=1)[0].strip()
                declared.add(name.replace("_", "-").lower())
        wanted: set[str] = set()
        for py in svc.glob("*.py"):
            try:
                wanted |= _shared_imports(ast.parse(py.read_text(encoding="utf-8")))
            except SyntaxError:
                continue
        seen: set[str] = set()
        while wanted - seen:
            mod = (wanted - seen).pop()
            seen.add(mod)
            if mod in trees:
                wanted |= _shared_imports(trees[mod])
        for mod in sorted(seen):
            for client in sorted(_cloud_clients(trees[mod])) if mod in trees else []:
                base = CLOUD_DIST.get(client, re.sub("_v[0-9]+$", "", client)).replace("_", "-")
                dist = f"google-cloud-{base}"
                if dist not in declared:
                    missing.append(f"{svc.name}: shared/{mod}.py opens google.cloud.{client} but requirements.txt lacks {dist}")
    if missing:
        add("shared-deps", "FAIL", f"{len(missing)} missing: " + " | ".join(sorted(set(missing))))
    else:
        add("shared-deps", "PASS", "every google.cloud client a shared module opens is pinned by its importers")


# ---- 4. requirements pinned -------------------------------------------------
def check_requirements() -> None:
    unpinned, files = [], list(SERVICES.rglob("requirements.txt"))
    for req in files:
        for ln in req.read_text(encoding="utf-8").splitlines():
            ln = ln.split("#", 1)[0].strip()
            if not ln:
                continue
            if "==" not in ln:
                unpinned.append(f"{req.parent.name}: {ln}")
    if not files:
        add("requirements", "WARN", "no requirements.txt found")
    elif unpinned:
        add("requirements", "WARN", f"{len(unpinned)} unpinned: " + ", ".join(unpinned[:6]))
    else:
        add("requirements", "PASS", f"{len(files)} files, all deps pinned")


# ---- 4b. the same package, the same version, in every service ---------------
def check_pin_consistency() -> None:
    """Pinned is not enough; two services can pin the same package differently.

    That is invisible until a demo crosses the two - one service on google-genai
    2.20 and one on 2.22, and the failure is a 400 from whichever is older, mid
    demo, blamed on the model. Cheaper to fail here.
    """
    import re as _re
    pat = _re.compile(r"^([A-Za-z0-9_.\-]+)(\[[\w,]+\])?==([\w.]+)\s*$")
    pins: dict[str, dict[str, str]] = {}
    files = list(SERVICES.rglob("requirements.txt")) + \
        list((SERVICES.parent / "shared").glob("requirements.txt"))
    for req in files:
        svc = req.parent.name
        for ln in req.read_text(encoding="utf-8").splitlines():
            m = pat.match(ln.split("#", 1)[0].strip())
            if m:
                pins.setdefault(m.group(1), {})[svc] = m.group(3)
    shared = {p: d for p, d in pins.items() if len(d) > 1}
    bad = {p: d for p, d in shared.items() if len(set(d.values())) > 1}
    if not shared:
        add("pins", "WARN", "no package is shared by two services")
    elif bad:
        detail = "; ".join(
            f"{p} (" + ", ".join(f"{s}={v}" for s, v in sorted(d.items())) + ")"
            for p, d in list(bad.items())[:4])
        add("pins", "FAIL", f"{len(bad)} package(s) pinned differently: {detail}")
    else:
        add("pins", "PASS",
            f"{len(shared)} shared packages agree across {len(files)} files")


# ---- 5. dockerfile presence -------------------------------------------------
def check_dockerfile() -> None:
    svcs = sorted(p for p in SERVICES.iterdir() if p.is_dir())
    missing = [s.name for s in svcs if not (s / "Dockerfile").exists()]
    if not missing:
        add("dockerfile", "PASS", f"{len(svcs)} services have a Dockerfile")
    else:
        add("dockerfile", "WARN", f"no Dockerfile: {', '.join(missing)} (deploy via `--source` buildpacks?)")


# ---- 6/7. terraform ---------------------------------------------------------
def tf_errors(out: str) -> str:
    """Terraform's `Error:` headlines, each with the `on <file> line <n>` it points at.

    The last line of a diagnostic is its explanation, not its address; a verdict that quotes
    only that ("Blocks of type "foo" are not expected here") sends the reader into every file."""
    lines = [ln.strip().lstrip("│").strip() for ln in out.splitlines()]
    found = []
    for i, ln in enumerate(lines):
        if ln.startswith("Error:"):
            where = next((l for l in lines[i + 1:i + 5] if l.startswith("on ")), "")
            found.append(f"{ln[6:].strip()} ({where.rstrip(':')})" if where else ln[6:].strip())
    if found:
        return " | ".join(found[:4]) + (f" (+{len(found) - 4} more)" if len(found) > 4 else "")
    return next((ln for ln in reversed(lines) if ln), "no output")


def check_terraform() -> None:
    tf = shutil.which("terraform")
    if not tf:
        add("terraform", "SKIP", "terraform not on PATH (CI runs it)")
        return
    code_f, out_f = run([tf, "fmt", "-check", "-recursive"], TERRAFORM)   # names the files
    code_i, out_i = run([tf, "init", "-backend=false", "-input=false", "-no-color"], TERRAFORM)
    if code_i != 0:
        add("terraform", "FAIL", "init failed: " + tf_errors(out_i), output=out_i)
        return
    code_v, out_v = run([tf, "validate", "-no-color"], TERRAFORM)
    if code_v != 0:
        add("terraform", "FAIL", tf_errors(out_v), output=out_v)
    elif code_f != 0:
        files = ", ".join(ln.strip() for ln in out_f.splitlines() if ln.strip().endswith(".tf"))
        _, diff = run([tf, "fmt", "-check", "-diff", "-recursive"], TERRAFORM)
        add("terraform", "WARN", f"valid, but `terraform fmt` would reformat: {files or out_f[:160]}",
            output=diff)
    else:
        add("terraform", "PASS", "fmt + validate clean")


def check_tflint() -> None:
    tl = shutil.which("tflint")
    if not tl:
        add("tflint", "SKIP", "tflint not on PATH (optional)")
        return
    code, out = run([tl, "--chdir", str(TERRAFORM), "--no-color"], TERRAFORM)
    add("tflint", "PASS" if code == 0 else "WARN", (out.splitlines()[-1][:160] if out else "clean"))


# ---- 8. docker build context (static) --------------------------------------
COPY_RX = re.compile(r"^\s*(?:COPY|ADD)\s+(?:--\S+\s+)*(.+?)\s+\S+\s*$", re.M)
# Build artefacts a LATER lesson produces: a Dockerfile that copies one cannot build in a
# dry run, and that is a WARN with the lesson's name, not a FAIL.
LATER_ARTEFACTS = {"documind-slm.gguf": "10.5's fine-tune export (make ... slm)",
                   "build": "11.4's make deploy-slm writes services/slm/build/ (the GGUF and the Modelfile the image copies); gitignored, so absent in a fresh checkout"}


def docker_context(svc: Path) -> tuple[Path, list[str]]:
    """Where a service's image builds from, read off its own COPY lines.

    rag-api, chat, ingest and admin copy `shared/` and `services/<name>/`, so they build from
    deploy/ - the context cloudbuild.yaml uses (`docker build -f services/rag-api/Dockerfile .`).
    The rest copy their own files and build from their directory. The first CI run of the
    public repo (2026-09-06) built every image from its service directory and the four
    shared-context images failed on COPY; this is the rule that run was missing.
    Returns (context, sources missing from it)."""
    text = (svc / "Dockerfile").read_text(encoding="utf-8")
    srcs = [s.strip('"') for m in COPY_RX.finditer(text) for s in m.group(1).split()]
    ctx = DEPLOY if any(s.startswith(("shared/", "services/")) for s in srcs) else svc
    missing = sorted({s for s in srcs if s != "." and not (ctx / s).exists()})
    return ctx, missing


def check_copy_paths() -> None:
    """Every COPY source resolves in the context the Dockerfile assumes - checked without
    Docker, so the local dry run catches the bug the CI runner found."""
    svcs = [s for s in sorted(SERVICES.iterdir()) if s.is_dir() and (s / "Dockerfile").exists()]
    bad, later = [], []
    for s in svcs:
        ctx, missing = docker_context(s)
        for m in missing:
            name = Path(m).name
            (later if name in LATER_ARTEFACTS else bad).append(
                f"{s.name}: {m} ({LATER_ARTEFACTS[name]})" if name in LATER_ARTEFACTS
                else f"{s.name}: {m} not in {ctx.name}/")
    if bad:
        add("copy-paths", "FAIL", " | ".join(bad))
    elif later:
        add("copy-paths", "WARN", f"{len(svcs) - len(later)} of {len(svcs)} Dockerfiles resolve; "
                                  "needs a later lesson's artefact: " + " | ".join(later))
    else:
        add("copy-paths", "PASS", f"{len(svcs)} Dockerfiles: every COPY source exists in its build context")


# ---- 9. docker build --------------------------------------------------------
FROM_RX = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.M | re.I)
# A dry run pulls and builds on the official python image, which the python services use.
# Any other base (vLLM, LiteLLM, Ollama) is a vendor runtime of several gigabytes, GPU-shaped
# or not runnable on the runner at all; those Dockerfiles are parsed and linted instead
# (`docker build --check`) - the check that catches a doubled line-continuation - and are
# built where they run, by Cloud Build on the live tier.
BUILD_BASES = ("python:",)


def base_image(svc: Path) -> str:
    m = FROM_RX.search((svc / "Dockerfile").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def check_docker() -> None:
    dk = shutil.which("docker")
    if not dk:
        add("docker", "SKIP", "docker not on PATH (CI builds images)")
        return
    svcs = [s for s in sorted(SERVICES.iterdir()) if s.is_dir() and (s / "Dockerfile").exists()]
    failed, warned, skipped, built, linted, outputs = [], [], [], [], [], []
    for s in svcs:
        ctx, missing = docker_context(s)
        dockerfile = (s / "Dockerfile").relative_to(ctx).as_posix()
        base = base_image(s)
        if not base.startswith(BUILD_BASES):
            code, out = run([dk, "build", "--check", "-f", dockerfile, "."], ctx)
            linted.append(f"{s.name} on {base}")
            if code != 0 and ("ERROR" in out or "parse error" in out):
                failed.append(f"{s.name}: {build_error(out)}")
                outputs.append(f"### {s.name}\n" + "\n".join(out.splitlines()[-60:]))
            elif code != 0 or "WARNING" in out:
                lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
                first = next((ln for ln in lines if "WARNING" in ln), lines[-1] if lines else "lint warnings")
                warned.append(f"{s.name}: {first[:160]}")
            continue
        if missing:                                  # copy-paths already said why
            skipped.append(s.name)
            continue
        code, out = run([dk, "build", "-q", "-t", f"documind-{s.name}:dryrun", "-f", dockerfile, "."], ctx)
        if code != 0:
            failed.append(f"{s.name}: {build_error(out)}")
            outputs.append(f"### {s.name}\n" + "\n".join(out.splitlines()[-60:]))
        else:
            built.append(s.name)
    note = f"; {len(linted)} linted on vendor bases ({', '.join(linted)})" if linted else ""
    note += f"; skipped {', '.join(skipped)} (an artefact a later lesson produces)" if skipped else ""
    if failed:
        add("docker", "FAIL", " | ".join(failed) + note, output="\n\n".join(outputs))
    elif warned:
        add("docker", "WARN", f"{len(built)} images build; lint: " + " | ".join(warned) + note)
    else:
        add("docker", "PASS", f"{len(built)} images build from their own contexts{note}")


def build_error(out: str) -> str:
    """The lines a failed `docker build` blames. BuildKit ends with `failed to solve: ...
    exit code: 1`, which names the step; the pip or apt line that actually failed is a few
    lines above it, and that is the one worth reading."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    hits = [ln for ln in lines if "ERROR" in ln or "Error" in ln]
    # BuildKit's own two closing lines only repeat the step; keep them when nothing else is red
    real = [ln for ln in hits if "did not complete successfully" not in ln and "failed to solve" not in ln]
    keep = (real or hits)[-3:] or lines[-2:]
    return " // ".join(ln[:200] for ln in keep) if keep else "build failed (no output)"


# ---- CI reporting -----------------------------------------------------------
def esc(s: str) -> str:
    """GitHub workflow-command escaping for a message value."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report_to_ci() -> None:
    """On GitHub Actions, say it where it can be read without the raw log: one annotation per
    WARN / FAIL (on the commit, on the PR, and in the public check-runs API - the job log
    itself needs admin rights) and the whole table as the job summary, with the failing tool's
    own output folded under it."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    for check, status, detail in results:
        if status in ("FAIL", "WARN"):
            level = "error" if status == "FAIL" else "warning"
            print(f"::{level} title=dry run {check}::{esc(detail[:1500])}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as f:
        f.write("### DocuMind dry run\n\n| check | result | detail |\n|---|---|---|\n")
        for check, status, detail in results:
            cell = detail.replace("|", "\\|")
            f.write(f"| {check} | {status} | {cell} |\n")
        for check, out in raw.items():
            f.write(f"\n<details><summary>{check}: tool output</summary>\n\n```\n{out[-8000:]}\n```\n\n</details>\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    args = ap.parse_args()

    import time
    seconds: dict[str, float] = {}
    for fn in (check_extract, check_pycompile, check_imports, check_shared_deps, check_requirements,
               check_pin_consistency,
               check_dockerfile, check_copy_paths, check_terraform, check_tflint, check_docker):
        t0, before = time.perf_counter(), len(results)
        try:
            fn()
        except Exception as e:                    # a broken check must not hide the rest
            add(fn.__name__.replace("check_", ""), "FAIL", f"harness error: {e}")
        for check, _, _ in results[before:]:      # the seconds each check took: on CI, where
            seconds[check] = time.perf_counter() - t0   # terraform and docker run, this is how a
                                                  # failing step is read without the raw log

    width = max(len(c) for c, _, _ in results)
    print("\n  DocuMind AI — Tier-A offline dry run")
    print("  " + "-" * 60)
    order = {"FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}
    for check, status, detail in results:
        print(f"  [{status}] {check.ljust(width)}  {detail}  ({seconds.get(check, 0):.0f}s)")
    print("  " + "-" * 60)
    counts = {k: sum(1 for _, s, _ in results if s == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    print(f"  {counts['PASS']} pass · {counts['WARN']} warn · {counts['FAIL']} fail · {counts['SKIP']} skip"
          f" · {sum(seconds.values()):.0f}s\n")
    report_to_ci()

    failed = counts["FAIL"] > 0 or (args.strict and counts["WARN"] > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
