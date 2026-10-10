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
    M = 1_000_000

    def test_estimate_matches_price_table(self):
        cost = token_stats.estimate_cost_usd(
            input_tokens=1_000_000, cache_creation=0, cache_read=0,
            output_tokens=0, model="claude-sonnet-5")
        self.assertAlmostEqual(cost, 2.0)

    def test_unknown_model_is_not_priced(self):
        self.assertIsNone(token_stats.estimate_cost_usd(self.M, 0, 0, 0, "mystery-model"))
        self.assertIsNone(token_stats.estimate_cost_usd(self.M, 0, 0, 0, None))
        # A future minor version must not fall back to its family neighbour.
        self.assertIsNone(token_stats.estimate_cost_usd(self.M, 0, 0, 0, "claude-opus-5-9"))

    def test_each_model_family_input_output(self):
        cases = {
            "claude-opus-5-5": (4.0, 20.0),
            "claude-opus-5": (5.0, 25.0),
            "claude-opus-4-8": (5.0, 25.0),
            "claude-opus-4-5-20251101": (5.0, 25.0),
            "claude-opus-4-1-20250805": (15.0, 75.0),
            "claude-opus-4-20250514": (15.0, 75.0),
            "claude-sonnet-5-5": (2.0, 10.0),
            "claude-sonnet-5": (2.0, 10.0),
            "claude-sonnet-4-6": (3.0, 15.0),
            "claude-sonnet-4-5-20250929": (3.0, 15.0),
            "claude-sonnet-4-20250514": (3.0, 15.0),
            "claude-haiku-5-5": (0.10, 0.50),
            "claude-haiku-4-5-20251001": (1.0, 5.0),
            "claude-3-5-haiku-20241022": (0.80, 4.0),
            "claude-fable-5-1": (10.0, 50.0),
            "claude-fable-5": (10.0, 50.0),
            "claude-mythos-5-1": (10.0, 50.0),
            "claude-opus-5-5[1m]": (4.0, 20.0),
        }
        for model, (inp, out) in cases.items():
            with self.subTest(model=model):
                # 50K tokens keeps Haiku 5.5 in its base (<=100K) tier.
                n = 50_000
                self.assertAlmostEqual(
                    token_stats.estimate_cost_usd(n, 0, 0, 0, model), inp * n / self.M)
                self.assertAlmostEqual(
                    token_stats.estimate_cost_usd(0, 0, 0, n, model), out * n / self.M)

    def test_cache_read_multipliers(self):
        cases = {
            "claude-opus-5-5": 4.0 * 0.05,
            "claude-sonnet-5-5": 2.0 * 0.05,
            "claude-fable-5-1": 10.0 * 0.025,
            "claude-mythos-5-1": 10.0 * 0.025,
            "claude-fable-5": 10.0 * 0.1,
            "claude-opus-5": 5.0 * 0.1,
            "claude-sonnet-5": 2.0 * 0.1,
            "claude-sonnet-4-6": 3.0 * 0.1,
            "claude-haiku-4-5": 1.0 * 0.1,
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(
                    token_stats.estimate_cost_usd(0, 0, self.M, 0, model), expected)

    def test_cache_write_5m_and_1h_split(self):
        # claude-opus-5: input $5 -> 5m $6.25, 1h $10 per MTok.
        only_5m = token_stats.estimate_cost_usd(0, self.M, 0, 0, "claude-opus-5")
        only_1h = token_stats.estimate_cost_usd(
            0, self.M, 0, 0, "claude-opus-5", cache_write_1h=self.M)
        mixed = token_stats.estimate_cost_usd(
            0, 2 * self.M, 0, 0, "claude-opus-5", cache_write_1h=self.M)
        self.assertAlmostEqual(only_5m, 6.25)
        self.assertAlmostEqual(only_1h, 10.0)
        self.assertAlmostEqual(mixed, 16.25)

    def test_haiku_5_5_tier_boundary(self):
        # Prompt = input + cache read + cache write; over 100,000 -> $0.50/$2.50.
        at_limit = token_stats.estimate_cost_usd(
            40_000, 30_000, 30_000, self.M, "claude-haiku-5-5")
        over = token_stats.estimate_cost_usd(
            40_000, 30_000, 30_001, self.M, "claude-haiku-5-5")
        self.assertAlmostEqual(at_limit, (40_000 * 0.10 + 30_000 * 0.10 * 1.25
                                          + 30_000 * 0.10 * 0.1 + self.M * 0.50) / self.M)
        self.assertAlmostEqual(over, (40_000 * 0.50 + 30_000 * 0.50 * 1.25
                                      + 30_001 * 0.50 * 0.1 + self.M * 2.50) / self.M)
        # Only Haiku 5.5 has the tier.
        self.assertAlmostEqual(
            token_stats.estimate_cost_usd(200_000, 0, 0, 0, "claude-haiku-4-5"), 0.2)


class TestCacheWriteBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _load(self, usage, model="claude-opus-5"):
        path = self.tmp / "s.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"id": "m1", "model": model, "usage": usage}}), encoding="utf-8")
        return token_stats.load_calls(path)[0]

    def test_breakdown_is_read(self):
        c = self._load({"cache_creation_input_tokens": 300,
                        "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                           "ephemeral_1h_input_tokens": 200}})
        self.assertEqual((c.cache_creation, c.cache_write_5m, c.cache_write_1h,
                          c.cache_write_unknown), (300, 100, 200, 0))

    def test_missing_breakdown_falls_back_to_5m_and_flags(self):
        c = self._load({"cache_creation_input_tokens": 300})
        self.assertEqual((c.cache_write_5m, c.cache_write_1h, c.cache_write_unknown),
                         (300, 0, 300))

    def test_report_shows_split_flag_and_warnings(self):
        projects = self.tmp / "p"
        slug = projects / "slug"
        slug.mkdir(parents=True)

        def line(msg_id, model, usage, sec):
            return json.dumps({"type": "assistant",
                               "timestamp": f"2026-01-01T00:00:0{sec}.000Z",
                               "message": {"id": msg_id, "model": model, "usage": usage}})

        (slug / "a.jsonl").write_text("\n".join([
            line("m1", "claude-opus-5", {
                "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_5m_input_tokens": 100,
                                   "ephemeral_1h_input_tokens": 200}}, 0),
            line("m2", "claude-sonnet-5", {"cache_creation_input_tokens": 50}, 1),
            line("m3", "mystery-1", {"input_tokens": 10}, 2),
        ]), encoding="utf-8")
        report = token_stats.build_report(token_stats.collect_sessions(projects))
        rows = {r["model"]: r for r in report["by_model"]}
        self.assertEqual((rows["claude-opus-5"]["cache_write_5m"],
                          rows["claude-opus-5"]["cache_write_1h"]), (100, 200))
        self.assertEqual(rows["claude-sonnet-5"]["cache_write_unknown"], 50)
        self.assertIsNone(rows["mystery-1"]["est_usd"])
        self.assertIsNone(rows["mystery-1"]["cost_pct"])
        self.assertEqual(report["cost_estimate_status"], "partial")
        self.assertEqual(report["est_total_usd"], round(
            (100 * 6.25 + 200 * 10.0) / 1e6 + 50 * 2.5 / 1e6, 2))
        text = token_stats.format_report_text(report, projects, None)
        self.assertIn("WARNING: model 'mystery-1' has no known price", text)
        self.assertIn("no 5m/1h breakdown", text)
        self.assertIn("fast mode", text)
        self.assertIn("50~", text)


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
