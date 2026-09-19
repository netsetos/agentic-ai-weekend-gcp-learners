#!/usr/bin/env python3
"""Prepare and review an existing deploy/terraform workspace without resetting it.

From deploy/: python commands/infrastructure.py plan --project YOUR_PROJECT --region asia-south1
First deployment also needs --github-repository OWNER/REPO --github-repository-id NUMBER
--deploy-ref refs/heads/BRANCH. These identify the CI repository, not the download repository.
Existing WIF trust is retained. Initialize the intended backend once before using this command.

plan discovers billing, saves confirmed inputs, creates a unique saved plan, and rejects
all deletes/replacements and changes to existing CI trust. check is read-only. apply
checks the same saved plan again; invoking the apply action is the operator's approval.
No action initializes, migrates, imports, destroys, or changes Terraform workspaces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid

INPUT_FILE = "runbook-project.auto.tfvars.json"
SELECTED_FILE = "runbook-selected-plan.json"
CRITICAL = ("project_id", "region", "billing_account_id", "github_repository",
            "github_repository_id", "deploy_ref")
TRUST = ("github_repository", "github_repository_id", "deploy_ref")
WIF_ADDRESS = "google_iam_workload_identity_pool_provider.github"


class Stop(RuntimeError):
    """An actionable precondition failed; do not continue to apply."""


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Stop(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise Stop(f"Expected a JSON object in {path}")
    return value


def write_json(path, value):
    """Private, atomic files: never leave half a tfvars file after interruption."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def billing_id(value):
    value = str(value or "").strip().removeprefix("billingAccounts/").upper()
    if not re.fullmatch(r"[A-Z0-9]{6}-[A-Z0-9]{6}-[A-Z0-9]{6}", value) or value == "000000-000000-000000":
        raise Stop("The project has no valid linked billing account; link billing before planning.")
    return value


def validate_trust(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.get("github_repository", "")):
        raise Stop("Set the CI repository as OWNER/REPO.")
    if not re.fullmatch(r"[1-9][0-9]*", value.get("github_repository_id", "")):
        raise Stop("Set the CI repository's immutable numeric GitHub ID.")
    if not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", value.get("deploy_ref", "")):
        raise Stop("Set the CI branch as refs/heads/BRANCH.")
    return {key: value[key] for key in TRUST}


def parse_trust(condition):
    """Accept only the exact three equalities that wif.tf can reproduce."""
    fields = {"repository": "github_repository", "repository_id": "github_repository_id", "ref": "deploy_ref"}
    result = {}
    for clause in str(condition or "").split("&&"):
        match = re.fullmatch(r"\s*assertion\.(repository_id|repository|ref)\s*==\s*(['\"])([^'\"]+)\2\s*", clause)
        if not match or fields[match[1]] in result:
            raise Stop("Existing WIF condition is not the supported three-gate policy. Review it without overwriting it.")
        result[fields[match[1]]] = match[3]
    return validate_trust(result)


def inspect_plan(plan, expected):
    """Validate values and actions, independently of Terraform's human plan text."""
    if plan.get("errored") or plan.get("complete") is False or plan.get("deferred_changes"):
        raise Stop("The Terraform plan is incomplete or errored. Resolve it and create a new plan.")
    actual = {key: plan.get("variables", {}).get(key, {}).get("value") for key in CRITICAL}
    if actual != {key: expected[key] for key in CRITICAL}:
        raise Stop("Saved plan inputs differ from confirmed project/billing/CI inputs. Create a new plan.")
    blocked = []
    for resource in plan.get("resource_changes", []):
        change = resource.get("change", {})
        address = resource.get("address", "unknown resource")
        if "delete" in change.get("actions", []):
            blocked.append(address + " (delete/replacement)")
        before, after = change.get("before"), change.get("after")
        if before and resource.get("type") == "google_iam_workload_identity_pool_provider":
            keys = ("attribute_condition", "attribute_mapping", "oidc", "disabled")
            if not after or any(before.get(key) != after.get(key) for key in keys):
                blocked.append(address + " (existing WIF trust change)")
        if before and address == "google_service_account_iam_member.cicd_wif":
            if not after or any(before.get(key) != after.get(key) for key in ("member", "role", "service_account_id")):
                blocked.append(address + " (existing CI binding change)")
    if blocked:
        raise Stop("Plan blocked: " + "; ".join(blocked) + ". Review Terraform configuration; do not delete data or remove state to bypass this check.")


class Infrastructure:
    def __init__(self, args, run=None):
        self.args = args
        self.directory = Path(args.terraform_dir).expanduser().resolve()
        self.inputs = self.directory / INPUT_FILE
        self.selected = self.directory / SELECTED_FILE
        self.run = run or subprocess.run
        if not self.directory.is_dir():
            raise Stop(f"Terraform directory does not exist: {self.directory}")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project or ""):
            raise Stop("Pass --project with the intended GCP project ID (or export PROJECT).")
        if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", args.region or ""):
            raise Stop("Pass --region with the intended deployment region (or export REGION).")
        self.environment = dict(os.environ, CLOUDSDK_CORE_PROJECT=args.project,
                                CLOUDSDK_BILLING_QUOTA_PROJECT=args.project,
                                GOOGLE_CLOUD_QUOTA_PROJECT=args.project)
        hidden = [key for key, value in self.environment.items() if key.startswith("TF_CLI_ARGS") and value]
        if hidden:
            raise Stop("Unset hidden Terraform command arguments before proceeding: " + ", ".join(hidden))

    def command(self, command, capture=True, allowed_failure=False):
        result = self.run(command, cwd=self.directory, env=self.environment, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, check=False)
        if result.returncode and not allowed_failure:
            detail = (result.stderr or "").strip() if capture else "See the command output above."
            raise Stop(f"{command[0]} {command[1]} failed ({result.returncode}). {detail}")
        return result

    def json_command(self, command):
        result = self.command(command)
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise Stop(f"{command[0]} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise Stop(f"{command[0]} returned an unexpected JSON response")
        return value

    def context(self):
        data_dir = Path(self.environment.get("TF_DATA_DIR", ".terraform")).expanduser()
        if not data_dir.is_absolute():
            data_dir = self.directory / data_dir
        metadata = data_dir / "terraform.tfstate"
        if not metadata.is_file():
            raise Stop("Initialize the intended Terraform backend first. This command never initializes or migrates state.")
        backend = read_json(metadata).get("backend")
        if not isinstance(backend, dict) or not backend.get("type"):
            raise Stop("Initialized backend metadata is missing. Verify the intended backend before continuing.")
        workspace = self.command(["terraform", "workspace", "show"]).stdout.strip()
        if not workspace:
            raise Stop("Terraform did not report its current workspace.")
        backend_hash = hashlib.sha256(json.dumps(backend, sort_keys=True).encode()).hexdigest()
        return {"directory": str(self.directory), "workspace": workspace, "backend_sha256": backend_hash}

    def state(self):
        result = self.command(["terraform", "state", "pull"], allowed_failure=True)
        if result.returncode:
            # Exact local absence only. Permission, network, backend and auth failures are not absence.
            if (result.stderr or "").strip() == "No state file was found!":
                return {"resources": []}
            raise Stop("Cannot read Terraform state. Fix backend/access errors; do not treat this as a new deployment. " + (result.stderr or "").strip())
        # Terraform exits successfully with no output when the backend has no state yet.
        if not (result.stdout or "").strip():
            return {"resources": []}
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise Stop("Terraform state was not valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("resources", []), list):
            raise Stop("Terraform returned an unexpected state document")
        return value

    def discover(self, state, saved):
        project = self.json_command(["gcloud", "projects", "describe", self.args.project, "--format=json"])
        if project.get("projectId") != self.args.project or project.get("lifecycleState") != "ACTIVE":
            raise Stop("The requested project is not active or its identity could not be verified.")
        number = str(project.get("projectNumber", ""))
        if not number.isdigit():
            raise Stop("Google Cloud did not return a valid project number.")
        billing = self.json_command(["gcloud", "billing", "projects", "describe", self.args.project, "--format=json"])
        if billing.get("projectId") != self.args.project or billing.get("billingEnabled") is not True:
            raise Stop("Billing is not enabled on the requested project, or the billing project response mismatched.")
        result = {"project_id": self.args.project, "region": self.args.region,
                  "billing_account_id": billing_id(billing.get("billingAccountName"))}
        existing = None
        for resource in state.get("resources", []):
            for instance in resource.get("instances", []):
                attributes = instance.get("attributes", {})
                if resource.get("mode") == "managed" and attributes.get("project") not in (None, "", self.args.project, number):
                    raise Stop("The selected state contains resources from another project. Verify backend/workspace; do not migrate it automatically.")
                if (resource.get("type"), resource.get("name"), resource.get("module")) == ("google_iam_workload_identity_pool_provider", "github", None):
                    if existing is not None:
                        raise Stop("Multiple WIF provider instances found; review state before planning.")
                    existing = parse_trust(attributes.get("attribute_condition"))
        if existing is None:
            probe = self.command(["gcloud", "iam", "workload-identity-pools", "providers", "describe", "github-oidc",
                                  "--workload-identity-pool=documind-github", "--location=global",
                                  f"--project={self.args.project}", "--format=json"], allowed_failure=True)
            if probe.returncode == 0:
                raise Stop("The GitHub WIF provider exists in Google Cloud but is absent from this state. Select its existing backend/workspace or review an import; do not recreate it.")
            error = probe.stderr or ""
            if not re.search(r"\bNOT_FOUND\b", error) or re.search(r"PERMISSION_DENIED|UNAUTHENTICATED|SERVICE_DISABLED|permission", error, re.I):
                raise Stop("Cannot establish that WIF is absent. Fix the provider lookup error before proceeding: " + error.strip())
        explicit = {key: getattr(self.args, key, None) for key in TRUST}
        if existing:
            if any(value is not None and value != existing[key] for key, value in explicit.items()):
                raise Stop("Requested CI inputs differ from existing WIF trust. Keep the existing repository identity; changing the download repository does not change CI trust.")
            result.update(existing)
        else:
            candidate = {key: explicit[key] if explicit[key] is not None else saved.get(key, "") for key in TRUST}
            result.update(validate_trust(candidate))
        return result

    def saved_inputs(self):
        saved = read_json(self.inputs) if self.inputs.exists() else {}
        if saved.get("project_id") not in (None, self.args.project):
            raise Stop("Saved inputs belong to a different project. Select the correct deployment directory/backend.")
        if saved.get("region") not in (None, self.args.region):
            raise Stop("Saved region differs. Region moves require a separate reviewed migration.")
        return saved

    def prepare(self):
        context = self.context()
        saved = self.saved_inputs()
        state = self.state()
        confirmed = self.discover(state, saved)
        write_json(self.inputs, {**saved, **confirmed})
        print(f"PASS: saved confirmed inputs to {self.inputs}")
        print(f"CI trust: {confirmed['github_repository']} (ID {confirmed['github_repository_id']}) / {confirmed['deploy_ref']}")
        return confirmed, context

    def plan(self):
        # A failed rerun must never leave an older plan selected for default apply.
        self.selected.unlink(missing_ok=True)
        confirmed, context = self.prepare()
        filename = "rag-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:10] + ".tfplan"
        path = self.directory / filename
        args = ["terraform", "plan", "-input=false", "-lock-timeout=5m", "-parallelism=3"]
        for item in self.args.var:
            key, separator, _ = item.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) or key in CRITICAL:
                raise Stop("--var requires NAME=VALUE for a noncritical input; critical inputs are discovered/confirmed separately.")
            args.extend(["-var", item])
        args += [f"-var-file={self.inputs}", f"-out={path}"]
        previous_umask = os.umask(0o077)
        try:
            self.command(args, capture=False)
        finally:
            os.umask(previous_umask)
        plan = self.json_command(["terraform", "show", "-json", str(path)])
        inspect_plan(plan, confirmed)
        if self.context() != context:
            raise Stop("Backend/workspace changed while planning. Create a new plan in the intended workspace.")
        current = self.state()
        record = {"plan": str(path), "sha256": digest(path), "inputs": confirmed, "context": context,
                  "state": {"lineage": current.get("lineage"), "serial": current.get("serial")}}
        write_json(path.with_suffix(path.suffix + ".checked.json"), record)
        write_json(self.selected, record)
        print(f"PASS: no deletes/replacements or existing CI trust changes. Reviewed plan: {path}")
        print("Review the displayed changes, then run this command with 'apply' instead of 'plan'.")

    def check(self):
        if self.args.plan:
            path = Path(self.args.plan).expanduser()
            if not path.is_absolute():
                path = self.directory / path
            record = read_json(path.with_suffix(path.suffix + ".checked.json"))
        else:
            record = read_json(self.selected)
            path = Path(record["plan"])
        if str(path.resolve()) != record.get("plan") or not path.is_file() or digest(path) != record.get("sha256"):
            raise Stop("The selected plan is missing or changed. Create a new plan.")
        if self.context() != record.get("context"):
            raise Stop("Backend/workspace/directory changed since planning. Create a new plan.")
        saved = self.saved_inputs()
        if {key: saved.get(key) for key in CRITICAL} != record.get("inputs"):
            raise Stop("Confirmed inputs changed since planning. Create a new plan.")
        current = self.state()
        stamp = {"lineage": current.get("lineage"), "serial": current.get("serial")}
        if stamp != record.get("state"):
            raise Stop("Terraform state changed since planning. The saved plan is stale; create a new plan.")
        if self.discover(current, saved) != record["inputs"]:
            raise Stop("Project billing/CI settings changed since planning. Create a new plan.")
        inspect_plan(self.json_command(["terraform", "show", "-json", str(path)]), record["inputs"])
        print(f"PASS: selected plan, confirmed inputs, backend/workspace and state agree: {path}")
        return path

    def apply(self):
        path = self.check()
        self.check()  # Recheck immediately before mutation; apply itself is explicit approval.
        self.selected.unlink(missing_ok=True)
        path.with_suffix(path.suffix + ".checked.json").unlink(missing_ok=True)
        self.command(["terraform", "apply", "-lock-timeout=5m", "-parallelism=3", str(path)], capture=False)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("prepare", "plan", "check", "apply"))
    parser.add_argument("--project", default=os.environ.get("PROJECT"))
    parser.add_argument("--region", default=os.environ.get("REGION"))
    default_root = Path(os.environ.get("DEMO_ROOT", Path(__file__).resolve().parents[1]))
    parser.add_argument("--terraform-dir", default=str(default_root / "terraform"))
    parser.add_argument("--plan", help="Previously checked saved plan, for check/apply; default: last successful plan")
    parser.add_argument("--github-repository", help="New deployment only: CI OWNER/REPO (independent of source clone)")
    parser.add_argument("--github-repository-id", help="New deployment only: CI repository's immutable numeric ID")
    parser.add_argument("--deploy-ref", help="New deployment only: CI refs/heads/BRANCH")
    parser.add_argument("--var", action="append", default=[], help="plan only: extra noncritical NAME=VALUE (repeatable)")
    result = parser.parse_args(argv)
    if result.plan and result.action not in ("check", "apply"):
        parser.error("--plan is only for check/apply; plan always creates a unique new file")
    if result.var and result.action != "plan":
        parser.error("--var is only supported for plan")
    return result


def main(argv=None):
    try:
        args = arguments(argv)
        getattr(Infrastructure(args), args.action)()
        return 0
    except (Stop, OSError, EOFError, KeyboardInterrupt) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
