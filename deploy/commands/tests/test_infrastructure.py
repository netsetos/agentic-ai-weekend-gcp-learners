"""Offline regression tests; fake Google Cloud and Terraform responses, no cloud writes."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("infrastructure", Path(__file__).resolve().parents[1] / "infrastructure.py")
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)

PROJECT = "documind-course-demo"
NUMBER = "123456789012"
TRUST = {"github_repository": "example/main", "github_repository_id": "12345", "deploy_ref": "refs/heads/demo"}
CONDITION = "assertion.repository_id == '12345' && assertion.repository == 'example/main' && assertion.ref == 'refs/heads/demo'"
INPUTS = {"project_id": PROJECT, "region": "asia-south1", "billing_account_id": "ABCDEF-123456-A1B2C3", **TRUST}
STATE = {"lineage": "state-lineage", "serial": 4, "resources": [{"mode": "managed", "type": "google_iam_workload_identity_pool_provider", "name": "github", "instances": [{"attributes": {"project": PROJECT, "attribute_condition": CONDITION}}]}]}
PLAN = {"format_version": "1.2", "variables": {key: {"value": value} for key, value in INPUTS.items()}, "resource_changes": []}


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.state = copy.deepcopy(STATE)
        self.plan = copy.deepcopy(PLAN)
        self.plan_code = 0
        self.billing = {"projectId": PROJECT, "billingEnabled": True, "billingAccountName": "billingAccounts/ABCDEF-123456-A1B2C3"}
        self.probe_code = 1
        self.probe_error = "NOT_FOUND: workload identity provider not found"
        self.state_error = None
        self.empty_state = False
        self.billing_error = None

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        code, stderr, output = 0, "", {}
        if command[:3] == ["terraform", "workspace", "show"]:
            return subprocess.CompletedProcess(command, 0, "default\n", "")
        if command[:3] == ["terraform", "state", "pull"]:
            if self.state_error:
                return subprocess.CompletedProcess(command, 1, "", self.state_error)
            if self.empty_state:
                return subprocess.CompletedProcess(command, 0, "", "")
            output = self.state
        elif command[:3] == ["gcloud", "projects", "describe"]:
            output = {"projectId": PROJECT, "projectNumber": NUMBER, "lifecycleState": "ACTIVE"}
        elif command[:4] == ["gcloud", "billing", "projects", "describe"]:
            if self.billing_error:
                return subprocess.CompletedProcess(command, 1, "", self.billing_error)
            output = self.billing
        elif command[:3] == ["gcloud", "iam", "workload-identity-pools"]:
            code, stderr = self.probe_code, self.probe_error
        elif command[:2] == ["terraform", "plan"]:
            code = self.plan_code
            for argument in command:
                if argument.startswith("-out="):
                    Path(argument[5:]).write_bytes(b"a-fake-saved-plan")
        elif command[:3] == ["terraform", "show", "-json"]:
            output = self.plan
        elif command[:2] != ["terraform", "apply"]:
            raise AssertionError(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, code, json.dumps(output), stderr)


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".terraform").mkdir()
        infra.write_json(self.root / ".terraform/terraform.tfstate", {"backend": {"type": "gcs", "config": {"bucket": "test-tfstate", "prefix": "documind/env"}}})
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fake = FakeCommands()

    def instance(self, action="prepare", extra=()):
        args = infra.arguments([action, "--project", PROJECT, "--region", "asia-south1", "--terraform-dir", str(self.root), *extra])
        return infra.Infrastructure(args, run=self.fake)

    def test_existing_trust_wins_over_saved_download_repository(self):
        infra.write_json(self.root / infra.INPUT_FILE, {"project_id": PROJECT, "budget_amount": "500", "github_repository": "example/learners", "github_repository_id": "99999", "deploy_ref": "refs/heads/other"})
        self.instance().prepare()
        saved = infra.read_json(self.root / infra.INPUT_FILE)
        self.assertEqual(saved, {**INPUTS, "budget_amount": "500"})
        self.assertEqual((self.root / infra.INPUT_FILE).stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(cmd[:3] == ["gcloud", "iam", "workload-identity-pools"] for cmd, _ in self.fake.calls))

    def test_empty_billing_environment_cannot_override_confirmed_file(self):
        os.environ["TF_VAR_billing_account_id"] = ""
        self.instance("plan").plan()
        command = next(cmd for cmd, _ in self.fake.calls if cmd[:2] == ["terraform", "plan"])
        self.assertIn(f"-var-file={self.root / infra.INPUT_FILE}", command)
        self.assertEqual(infra.read_json(self.root / infra.INPUT_FILE)["billing_account_id"], INPUTS["billing_account_id"])
        self.assertTrue((self.root / infra.SELECTED_FILE).is_file())

    def test_invalid_billing_stops_before_writing(self):
        for value in ("", "000000-000000-000000", "not-an-account"):
            with self.subTest(value=value):
                self.fake.billing["billingAccountName"] = value
                with self.assertRaises(infra.Stop):
                    self.instance().prepare()
                self.assertFalse((self.root / infra.INPUT_FILE).exists())

    def test_disabled_billing_and_wrong_project_stop(self):
        self.fake.billing["billingEnabled"] = False
        with self.assertRaises(infra.Stop):
            self.instance().prepare()
        self.fake.billing["billingEnabled"] = True
        self.fake.billing["projectId"] = "another-project"
        with self.assertRaises(infra.Stop):
            self.instance().prepare()

    def test_billing_permission_error_is_not_absence(self):
        self.fake.billing_error = "PERMISSION_DENIED: billing lookup denied"
        with self.assertRaisesRegex(infra.Stop, "PERMISSION_DENIED"):
            self.instance().prepare()

    def test_successful_empty_state_is_new_backend(self):
        self.fake.empty_state = True
        flags = ["--github-repository", TRUST["github_repository"], "--github-repository-id", TRUST["github_repository_id"], "--deploy-ref", TRUST["deploy_ref"]]
        self.instance(extra=flags).prepare()
        self.assertEqual(infra.read_json(self.root / infra.INPUT_FILE), INPUTS)

    def test_state_permission_error_is_not_new_deployment(self):
        self.fake.state_error = "403 access denied"
        with self.assertRaisesRegex(infra.Stop, "Cannot read Terraform state"):
            self.instance().prepare()
        self.assertFalse(any(cmd[:1] == ["gcloud"] for cmd, _ in self.fake.calls))

    def test_wif_permission_error_is_not_absence(self):
        self.fake.state = {"resources": []}
        self.fake.probe_error = "PERMISSION_DENIED: cannot access provider (or it may not exist)"
        with self.assertRaisesRegex(infra.Stop, "Cannot establish that WIF is absent"):
            self.instance().prepare()

    def test_existing_unmanaged_cloud_provider_requires_state_review(self):
        self.fake.state = {"resources": []}
        self.fake.probe_code = 0
        with self.assertRaisesRegex(infra.Stop, "absent from this state"):
            self.instance().prepare()

    def test_new_deployment_persists_explicit_ci_for_next_run(self):
        self.fake.state = {"resources": []}
        flags = ["--github-repository", TRUST["github_repository"], "--github-repository-id", TRUST["github_repository_id"], "--deploy-ref", TRUST["deploy_ref"]]
        self.instance(extra=flags).prepare()
        self.instance("plan").plan()
        self.assertEqual(infra.read_json(self.root / infra.INPUT_FILE), INPUTS)

    def test_existing_trust_cannot_be_switched_by_flags(self):
        with self.assertRaisesRegex(infra.Stop, "differ from existing WIF trust"):
            self.instance(extra=["--github-repository", "example/learners"]).prepare()

    def test_plan_blocks_datastore_replacement(self):
        self.fake.plan["resource_changes"] = [{"address": 'google_discovery_engine_data_store.tenant["acme"]', "type": "google_discovery_engine_data_store", "change": {"actions": ["delete", "create"]}}]
        with self.assertRaisesRegex(infra.Stop, "delete/replacement"):
            self.instance("plan").plan()
        self.assertFalse((self.root / infra.SELECTED_FILE).exists())

    def test_failed_plan_clears_old_selection(self):
        infra.write_json(self.root / infra.SELECTED_FILE, {"plan": "old.tfplan"})
        self.fake.plan_code = 1
        with self.assertRaises(infra.Stop):
            self.instance("plan").plan()
        self.assertFalse((self.root / infra.SELECTED_FILE).exists())
        self.assertFalse(list(self.root.glob("*.checked.json")))

    def test_plan_blocks_existing_wif_condition_change(self):
        self.fake.plan["resource_changes"] = [{"address": infra.WIF_ADDRESS, "type": "google_iam_workload_identity_pool_provider", "change": {"actions": ["update"], "before": {"attribute_condition": CONDITION}, "after": {"attribute_condition": CONDITION.replace("12345", "99999")}}}]
        with self.assertRaisesRegex(infra.Stop, "WIF trust change"):
            self.instance("plan").plan()

    def test_check_refuses_stale_state_without_apply(self):
        self.instance("plan").plan()
        self.fake.state["serial"] += 1
        with self.assertRaisesRegex(infra.Stop, "stale"):
            self.instance("check").check()
        self.assertFalse(any(cmd[:2] == ["terraform", "apply"] for cmd, _ in self.fake.calls))

    def test_check_refuses_changed_plan_bytes(self):
        self.instance("plan").plan()
        selected = infra.read_json(self.root / infra.SELECTED_FILE)
        Path(selected["plan"]).write_bytes(b"changed")
        with self.assertRaisesRegex(infra.Stop, "missing or changed"):
            self.instance("check").check()

    def test_check_and_exact_apply(self):
        self.instance("plan").plan()
        saved = infra.read_json(self.root / infra.SELECTED_FILE)
        self.instance("apply").apply()
        command = next(cmd for cmd, _ in self.fake.calls if cmd[:2] == ["terraform", "apply"])
        self.assertEqual(command[-1], saved["plan"])
        self.assertNotIn("-auto-approve", command)
        self.assertFalse((self.root / infra.SELECTED_FILE).exists())

    def test_critical_plan_vars_rejected(self):
        with self.assertRaisesRegex(infra.Stop, "noncritical"):
            self.instance("plan", ["--var", "project_id=another-project"]).plan()

    def test_unknown_wif_policy_cannot_be_simplified(self):
        with self.assertRaisesRegex(infra.Stop, "three-gate policy"):
            infra.parse_trust(CONDITION + " && assertion.actor == 'alice'")


if __name__ == "__main__":
    unittest.main()
