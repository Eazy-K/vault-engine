"""Tests for tools/token_stats.py (read-only Claude Code token usage report).

Uses only tempfile-based fake transcript trees; never touches the real
~/.claude/projects directory. Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

TOOLS_DIR = str(Path(__file__).resolve().parent.parent / "tools")
sys.path.insert(0, TOOLS_DIR)

_spec = importlib.util.spec_from_file_location(
    "token_stats", Path(TOOLS_DIR) / "token_stats.py")
token_stats = importlib.util.module_from_spec(_spec)
sys.modules["token_stats"] = token_stats
_spec.loader.exec_module(token_stats)


def assistant_line(msg_id, model, ts, input_t=0, cache_w=0, cache_r=0, output_t=0):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": input_t,
                "cache_creation_input_tokens": cache_w,
                "cache_read_input_tokens": cache_r,
                "output_tokens": output_t,
            },
        },
    })


class TestLoadCalls(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dedupes_by_message_id_keeping_last(self):
        path = self.tmp / "s.jsonl"
        path.write_text("\n".join([
            assistant_line("m1", "claude-sonnet-5", "2026-01-01T00:00:00.000Z",
                            input_t=10, output_t=1),
            assistant_line("m1", "claude-sonnet-5", "2026-01-01T00:00:01.000Z",
                            input_t=10, output_t=5),
            assistant_line("m2", "claude-sonnet-5", "2026-01-01T00:00:02.000Z",
                            input_t=20, output_t=2),
        ]), encoding="utf-8")
        calls = token_stats.load_calls(path)
        self.assertEqual([c.id for c in calls], ["m1", "m2"])
        self.assertEqual(calls[0].output, 5)  # last occurrence wins

    def test_skips_synthetic_messages(self):
        path = self.tmp / "s.jsonl"
        path.write_text("\n".join([
            assistant_line("m1", "<synthetic>", "2026-01-01T00:00:00.000Z"),
            assistant_line("m2", "claude-sonnet-5", "2026-01-01T00:00:01.000Z",
                            input_t=10),
        ]), encoding="utf-8")
        self.assertEqual([c.id for c in token_stats.load_calls(path)], ["m2"])

    def test_ignores_non_assistant_and_malformed_lines(self):
        path = self.tmp / "s.jsonl"
        path.write_text("\n".join([
            json.dumps({"type": "user", "message": {"id": "u1"}}),
            "not json",
            assistant_line("m1", "claude-sonnet-5", "2026-01-01T00:00:00.000Z", input_t=1),
        ]), encoding="utf-8")
        calls = token_stats.load_calls(path)
        self.assertEqual([c.id for c in calls], ["m1"])

    def test_since_filter_drops_older_and_undated_lines(self):
        path = self.tmp / "s.jsonl"
        path.write_text("\n".join([
            assistant_line("old", "claude-sonnet-5", "2026-01-01T00:00:00.000Z", input_t=1),
            assistant_line("new", "claude-sonnet-5", "2026-02-01T00:00:00.000Z", input_t=1),
        ]), encoding="utf-8")
        calls = token_stats.load_calls(path, since=date(2026, 1, 15))
        self.assertEqual([c.id for c in calls], ["new"])


class TestPercentile(unittest.TestCase):
    def test_median_and_p90_known_values(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(token_stats.percentile(values, 0.5), 5.5)
        self.assertAlmostEqual(token_stats.percentile(values, 0.9), 9.1)

    def test_empty_is_zero(self):
        self.assertEqual(token_stats.percentile([], 0.9), 0.0)


class TestCost(unittest.TestCase):
    def test_estimate_matches_price_table(self):
        cost = token_stats.estimate_cost_usd(
            input_tokens=1_000_000, cache_creation=0, cache_read=0,
            output_tokens=0, model="claude-sonnet-5")
        self.assertAlmostEqual(cost, 2.0)

    def test_unknown_model_costs_zero(self):
        cost = token_stats.estimate_cost_usd(1_000_000, 0, 0, 0, "mystery-model")
        self.assertEqual(cost, 0.0)


class TestDiscoverAndReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        slug = self.projects / "C--proj"
        slug.mkdir(parents=True)

        # Main session with a growing-then-shrinking context.
        (slug / "sess1.jsonl").write_text("\n".join([
            assistant_line("a1", "claude-sonnet-5", "2026-01-01T00:00:00.000Z",
                            input_t=100, cache_w=0, cache_r=0, output_t=10),
            assistant_line("a2", "claude-sonnet-5", "2026-01-01T00:01:00.000Z",
                            input_t=50, cache_w=200, cache_r=0, output_t=20),
        ]), encoding="utf-8")

        # Second, smaller main session so top-N ordering is meaningful.
        (slug / "sess2.jsonl").write_text(
            assistant_line("b1", "claude-opus-4", "2026-01-01T00:00:00.000Z",
                            input_t=5, cache_w=0, cache_r=0, output_t=1),
            encoding="utf-8")

        # A subagent run of sess1, with its meta.json giving the agent type.
        sub_dir = slug / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-x1.jsonl").write_text("\n".join([
            assistant_line("s1", "claude-haiku-4", "2026-01-01T00:00:30.000Z",
                            input_t=3, output_t=1),
            assistant_line("s2", "claude-haiku-4", "2026-01-01T00:00:31.000Z",
                            input_t=3, output_t=1),
        ]), encoding="utf-8")
        (sub_dir / "agent-x1.meta.json").write_text(
            json.dumps({"agentType": "worker-medium"}), encoding="utf-8")

    def test_discover_finds_main_and_subagent_with_type(self):
        found = list(token_stats.discover_transcripts(self.projects))
        kinds = {(kind, sid) for kind, _slug, sid, _path, _atype in found}
        self.assertIn(("main", "sess1"), kinds)
        self.assertIn(("main", "sess2"), kinds)
        sub = [f for f in found if f[0] == "subagent"]
        self.assertEqual(len(sub), 1)
        self.assertEqual(sub[0][4], "worker-medium")

    def test_report_totals_and_top_sessions(self):
        sessions = token_stats.collect_sessions(self.projects)
        report = token_stats.build_report(sessions, top=5)
        self.assertEqual(report["main_sessions"]["count"], 2)
        self.assertEqual(report["subagents"]["count"], 1)
        top_ids = [row["session_id"] for row in report["top_main_sessions"]]
        self.assertEqual(top_ids[0], "sess1")  # 380 tokens total beats sess2's 6
        sess1_row = report["top_main_sessions"][0]
        self.assertEqual(sess1_row["first_context"], 100)
        self.assertEqual(sess1_row["peak_context"], 250)
        self.assertEqual(sess1_row["total_tokens"], 380)
        agent_row = report["subagent_turns_by_type"][0]
        self.assertEqual(agent_row["agent_type"], "worker-medium")
        self.assertEqual(agent_row["n"], 1)
        self.assertEqual(agent_row["max_turns"], 2)

    def test_run_text_and_json_do_not_leak_message_content(self):
        text = token_stats.run(self.projects, top=5, as_json=False)
        self.assertIn("Main sessions:", text)
        self.assertNotIn("not json", text)
        parsed = json.loads(token_stats.run(self.projects, top=5, as_json=True))
        self.assertEqual(parsed["main_sessions"]["count"], 2)

    def test_since_filter_excludes_older_session(self):
        sessions = token_stats.collect_sessions(self.projects, since=date(2026, 6, 1))
        report = token_stats.build_report(sessions, top=5)
        self.assertEqual(report["main_sessions"]["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
