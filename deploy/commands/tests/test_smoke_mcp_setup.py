"""Offline checks for MCP smoke setup and authenticated outsider refusals."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, patch


SOURCE = Path(__file__).resolve().parents[2] / "smoke" / "smoke_mcp.py"
spec = importlib.util.spec_from_file_location("smoke_mcp_under_test", SOURCE)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
MEMBER = "documind-ui-sa@test-project.iam.gserviceaccount.com"
OUTSIDER = "documind-outsider-sa@test-project.iam.gserviceaccount.com"


class McpSmokeSetupTests(unittest.TestCase):
    def setUp(self):
        values = {"MCP_URL": "https://mcp-audience.example.test", "IMPERSONATE": MEMBER,
                  "OUTSIDER": OUTSIDER, "TENANT": "acme", "QUESTION": "The known fixture question?",
                  "passed": [], "failed": []}
        for name, value in values.items():
            patcher = patch.object(smoke, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def minted(token):
        return subprocess.CompletedProcess(["gcloud"], 0, token + "\n", "")

    @staticmethod
    def mint_failure():
        return subprocess.CalledProcessError(1, ["gcloud"], stderr="permission denied minting identity")

    def run_case(self, mint_results, tool_results):
        response = io.BytesIO(b'{"status":"ok"}')
        response.status = 200
        output = io.StringIO()
        with patch.object(smoke.subprocess, "run", side_effect=mint_results) as mint, \
                patch.object(smoke.urllib.request, "urlopen", return_value=response) as http, \
                patch.object(smoke, "call", new_callable=AsyncMock, side_effect=tool_results) as tools, \
                contextlib.redirect_stdout(output):
            status = smoke.main()
        return status, mint, http, tools, output.getvalue()

    @staticmethod
    def member_results():
        return [list(smoke.TOOLS), {"answerable": True, "citations": [{"source_uri": "fixture"}], "answer": "Known answer."}]

    def test_member_mint_failure_stops_before_http_or_tool_calls(self):
        status, mint, http, tools, output = self.run_case([self.mint_failure()], [])
        self.assertNotEqual(status, 0)
        self.assertEqual(mint.call_count, 1)
        http.assert_not_called()
        tools.assert_not_awaited()
        self.assertIn("no MCP requests were made", output)

    def test_outsider_mint_failure_never_sends_unauthenticated_tool_call(self):
        status, mint, http, tools, output = self.run_case(
            [self.minted("member-token"), self.mint_failure()], self.member_results())
        self.assertNotEqual(status, 0)
        self.assertEqual(mint.call_count, 2)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(tools.await_count, 2)
        self.assertTrue(all(call.args[0] == "member-token" for call in tools.await_args_list))
        self.assertIn("tenant-roster refusal was not tested", output)
        self.assertNotIn("outsider refused", smoke.passed)

    def test_authenticated_roster_refusal_passes(self):
        denied = RuntimeError(f"{OUTSIDER} is not on tenant 'acme''s roster")
        status, _, _, tools, _ = self.run_case(
            [self.minted("member-token"), self.minted("outsider-token")], self.member_results() + [denied])
        self.assertEqual(status, 0)
        self.assertEqual(tools.await_args_list[-1].args[0], "outsider-token")
        self.assertIn("outsider refused", smoke.passed)

    def test_authentication_refusal_is_not_a_roster_pass(self):
        denied = RuntimeError("not authenticated: token has the wrong audience; roster was not checked")
        status, _, _, tools, _ = self.run_case(
            [self.minted("member-token"), self.minted("outsider-token")], self.member_results() + [denied])
        self.assertNotEqual(status, 0)
        self.assertEqual(tools.await_args_list[-1].args[0], "outsider-token")
        self.assertIn("outsider refused", smoke.failed)
        self.assertNotIn("outsider refused", smoke.passed)

    def test_help_uses_activated_venv_install_and_region_placeholder(self):
        self.assertIn("Activate the runbook virtual environment", smoke.__doc__)
        self.assertIn("    python -m pip install 'fastmcp==3.4.7'", smoke.__doc__)
        self.assertNotIn("--user", smoke.__doc__)
        self.assertNotIn("`", smoke.__doc__)
        self.assertIn("NUMBER.REGION.run.app", smoke.__doc__)


if __name__ == "__main__":
    unittest.main()
