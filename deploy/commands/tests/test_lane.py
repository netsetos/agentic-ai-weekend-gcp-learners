"""Offline checks for commands/lane.py: the parser, the roster plan, and the index-name resolution order.

Run: python -m unittest discover -s commands/tests -p test_lane.py
No cloud client is imported: lane.py imports them inside the subcommands it runs.
"""
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lane", ROOT / "commands" / "lane.py")
lane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane)


class LaneTests(unittest.TestCase):
    def test_every_make_target_has_a_subcommand(self):
        ap = lane.build_parser()
        names = {a.dest for a in ap._actions}
        sub = next(a for a in ap._actions if a.dest == "cmd")
        self.assertEqual(set(sub.choices), {"sources", "queued", "vector-status", "backfill-vectors", "roster",
                                            "tenant-backend", "tenant-policy"})
        self.assertIn("project", names)

    def test_roster_plan_is_the_make_recipe(self):
        plan, policies = lane.roster_plan("p", "acme", ["a@x.com", "b@y.com"])
        self.assertEqual(plan[:2], [("acme", "a@x.com"), ("acme", "b@y.com")])
        for name in ("ui", "mcp", "chat"):
            for t in ("acme", "zeta", "globex"):
                self.assertIn((t, f"documind-{name}-sa@p.iam.gserviceaccount.com"), plan)
        self.assertIn(("acme", "documind-agent-sa@p.iam.gserviceaccount.com"), plan)
        self.assertNotIn(("zeta", "documind-agent-sa@p.iam.gserviceaccount.com"), plan)
        self.assertEqual(len(plan), 2 + 9 + 1)
        self.assertEqual(policies, [("acme", "any"), ("zeta", "any"), ("globex", "in")])

    def test_roster_dry_run_prints_the_plan_without_a_project(self):
        out = io.StringIO()
        with redirect_stdout(out), patch.dict(os.environ, {"PROJECT": "", "GOOGLE_CLOUD_PROJECT": ""}):
            code = lane.main(["roster", "--tenant", "acme", "--members", "a@x.com", "--dry-run"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("would put a@x.com on acme", text)
        self.assertIn("would set globex: data_region=in", text)
        self.assertEqual(text.count("would put"), 1 + 9 + 1)

    def test_index_name_prefers_the_shell_then_the_sdk_then_terraform(self):
        with patch.dict(os.environ, {"VECTOR_INDEX_NAME": "projects/1/locations/r/indexes/9"}):
            self.assertEqual(lane.index_name("p", "r"), "projects/1/locations/r/indexes/9")
        with patch.dict(os.environ, {"VECTOR_INDEX_NAME": ""}), \
                patch.object(lane, "_by_display_name", return_value="") as by_name, \
                patch.object(lane, "_terraform_output", return_value="from-terraform") as tf:
            self.assertEqual(lane.index_name("p", "r"), "from-terraform")
            by_name.assert_called_once_with("index", "documind-chunks", "p", "r")
            tf.assert_called_once_with("vector_index_name")

    def test_a_project_is_required_for_everything_else(self):
        with patch.dict(os.environ, {"PROJECT": "", "GOOGLE_CLOUD_PROJECT": ""}):
            with self.assertRaises(SystemExit):
                lane.main(["queued"])


if __name__ == "__main__":
    unittest.main()
