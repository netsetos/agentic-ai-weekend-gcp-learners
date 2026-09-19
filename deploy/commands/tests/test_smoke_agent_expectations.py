"""Offline regression checks for question-specific A2A smoke expectations."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "smoke" / "smoke_agent.py"
spec = importlib.util.spec_from_file_location("smoke_agent_under_test", SOURCE)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class AgentSmokeExpectationTests(unittest.TestCase):
    def accepts(self, question, text, status=200, response=None):
        if response is None:
            response = {"result": {"status": {"state": "completed"}}}
        return smoke.task_answer_matches(status, response, text, smoke.expected_answer_pattern(question))

    def test_probation_accepts_its_value_with_markdown_and_number_variants(self):
        for text in ("New joiners serve **six months** on probation.", "The period is 6 months.",
                     "A **six**-**month** probation.", "six (6) months", "SIX MONTHS"):
            with self.subTest(text=text):
                self.assertTrue(self.accepts(smoke.PROBATION_QUESTION, text))

    def test_probation_rejects_other_durations_and_empty_answers(self):
        for text in ("five years", "five months", "six weeks", "sixteen months", "66 months", "", "   "):
            with self.subTest(text=text):
                self.assertFalse(self.accepts(smoke.PROBATION_QUESTION, text))

    def test_gratuity_still_requires_five_years(self):
        for text in ("five years", "5 years", "five (5) years", "a five-year period"):
            with self.subTest(text=text):
                self.assertTrue(self.accepts(smoke.DEFAULT_QUESTION, text))
        for text in ("six months", "five months", "fifteen years", "15 years", "five eligible employees"):
            with self.subTest(text=text):
                self.assertFalse(self.accepts(smoke.DEFAULT_QUESTION, text))

    def test_question_lookup_normalizes_only_case_whitespace_and_question_mark(self):
        self.assertTrue(self.accepts("  WHAT is the probation period in the Acme HR policy  ", "six months"))
        with self.assertRaises(ValueError):
            smoke.expected_answer_pattern("What is the probation period in the Zeta HR policy?")

    def test_known_question_does_not_inherit_another_questions_expected_pattern(self):
        expected = smoke.expected_answer_pattern(smoke.PROBATION_QUESTION, r"five years")
        self.assertIsNotNone(expected.search("six months"))
        self.assertIsNone(expected.search("five years"))

    def test_custom_question_requires_an_explicit_valid_pattern(self):
        with self.assertRaises(ValueError):
            smoke.expected_answer_pattern("Where is the smoke lantern?")
        with self.assertRaises(ValueError):
            smoke.expected_answer_pattern("", "six months")
        with self.assertRaises(re.error):
            smoke.expected_answer_pattern("A custom question?", "[")
        with self.assertRaises(ValueError):
            smoke.expected_answer_pattern("A custom question?", ".*")
        expected = smoke.expected_answer_pattern("Where is the smoke lantern?", r"\bbay 7\b")
        self.assertTrue(smoke.task_answer_matches(200, {"result": {"status": {"state": "completed"}}},
                                                 "In BAY 7.", expected))

    def test_completed_http_success_without_rpc_error_is_required(self):
        expected = smoke.expected_answer_pattern(smoke.PROBATION_QUESTION)
        for state in ("working", "failed", "canceled", "input-required", None):
            with self.subTest(state=state):
                self.assertFalse(smoke.task_answer_matches(200, {"result": {"status": {"state": state}}},
                                                          "six months", expected))
        valid = {"result": {"status": {"state": "completed"}}}
        for status in (401, 403, 500):
            self.assertFalse(smoke.task_answer_matches(status, valid, "six months", expected))
        self.assertFalse(smoke.task_answer_matches(200, dict(valid, error={"message": "failed"}),
                                                  "six months", expected))

    def test_malformed_or_message_result_is_not_a_completed_task(self):
        for response in (None, [], {}, {"result": None}, {"result": []}, {"result": {"status": None}},
                         {"result": {"kind": "message", "role": "agent"}}):
            with self.subTest(response=response):
                expected = smoke.expected_answer_pattern(smoke.PROBATION_QUESTION)
                self.assertFalse(smoke.task_answer_matches(200, response, "six months", expected))

    def test_invalid_custom_configuration_stops_before_network(self):
        with patch.object(smoke, "AGENT_URL", "https://agent.example.test"), \
                patch.object(smoke, "QUESTION", "An unknown question?"), \
                patch.dict(os.environ, {"DOCUMIND_SMOKE_EXPECTED_PATTERN": ""}), \
                patch.object(smoke, "http") as http, patch.object(smoke, "token_as") as mint, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(), 2)
        http.assert_not_called()
        mint.assert_not_called()

    def test_reported_probation_answer_passes_full_smoke_flow_offline(self):
        url = "https://agent.example.test"
        answer = "According to the Acme HR policy (`hr_policy_2026.md`): New joiners serve **six months**."
        output = io.StringIO()
        with patch.object(smoke, "AGENT_URL", url), patch.object(smoke, "QUESTION", smoke.PROBATION_QUESTION), \
                patch.object(smoke, "IMPERSONATE", "member@test.iam.gserviceaccount.com"), \
                patch.object(smoke, "OUTSIDER", "outsider@test.iam.gserviceaccount.com"), \
                patch.object(smoke, "passed", []), patch.object(smoke, "failed", []), \
                patch.object(smoke, "token_as", side_effect=["member-token", "outsider-token"]), \
                patch.object(smoke, "http", side_effect=[(403, {}), (200, {"url": url})]), \
                patch.object(smoke, "send", side_effect=[
                    (200, {"result": {"status": {"state": "completed"}}}, answer),
                    (200, {}, "Access refused because this account is not on tenant zeta's roster."),
                    (403, {}, "Forbidden")]), contextlib.redirect_stdout(output):
            self.assertEqual(smoke.main(), 0)
        self.assertIn("5 passed, 0 failed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
