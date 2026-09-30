"""Tests for the Codex CLI section of tools/token_stats.py.

Uses only tempfile-based fake rollout trees; never touches the real
~/.codex/sessions directory. Run with:
    python -m unittest tests.test_token_stats_codex -v
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


def session_meta_line(session_id, ts, **extra):
    payload = {"id": session_id, "timestamp": ts, "cwd": "/x",
               "originator": "codex_cli", **extra}
    return json.dumps({
        "timestamp": ts,
        "type": "session_meta",
        "payload": payload,
    })


def turn_context_line(model, effort, turn_id="tu1"):
    payload = {"model": model, "effort": effort}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return json.dumps({
        "timestamp": "2026-09-27T14:12:51.000Z",
        "type": "turn_context",
        "payload": payload,
    })


def token_count_line(total_token_usage, rate_limits=None, info_null=False):
    info = None if info_null else {"total_token_usage": total_token_usage}
    payload = {"type": "token_count", "info": info}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return json.dumps({
        "timestamp": "2026-09-27T14:12:52.000Z",
        "type": "event_msg",
        "payload": payload,
    })


def token_usage_record_line(response_id, usage, ts="2026-09-25T10:48:28.946Z",
                            turn_id="tu1", thread_id="t1"):
    return json.dumps({
        "timestamp": ts,
        "ordinal": 15,
        "type": "token_usage_record",
        "payload": {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "session_id": "s1",
            "response_id": response_id,
            "usage": usage,
            "turn_token_usage": usage,
        },
    })


def make_rollout(root: Path, y, m, d, name, lines):
    day_dir = root / f"{y:04d}" / f"{m:02d}" / f"{d:02d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestParseCodexRollout(unittest.TestCase):
    def test_cached_input_is_removed_from_regular_input_price(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 10, "output_tokens": 5}
        # (100 - 20) * $0.10 + 20 * $0.01 + 10 * $0.125 + 5 * $0.50
        self.assertAlmostEqual(
            token_stats.estimate_codex_cost_usd(usage, "gpt-6-luna"),
            11.95 / 1_000_000)

    def test_cached_input_above_input_does_not_create_negative_regular_cost(self):
        usage = {"input_tokens": 10, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 0, "output_tokens": 0}
        expected = 20 * token_stats.CODEX_STANDARD_RATES["gpt-6-luna"][1]
        self.assertAlmostEqual(
            token_stats.estimate_codex_cost_usd(usage, "gpt-6-luna"),
            expected / 1_000_000)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_last_token_count_wins(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-27T14:12:50.597Z"),
            turn_context_line("gpt-6-luna", "medium"),
            token_count_line({"input_tokens": 10, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 1,
                               "reasoning_output_tokens": 0, "total_tokens": 11}),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 5,
                               "cache_write_input_tokens": 0, "output_tokens": 20,
                               "reasoning_output_tokens": 3, "total_tokens": 125},
                              rate_limits={"primary": {"used_percent": 10.0,
                                                        "window_minutes": 300,
                                                        "resets_at": "x"},
                                           "secondary": {"used_percent": 20.0,
                                                         "window_minutes": 10080,
                                                         "resets_at": "y"},
                                           "plan_type": "plus"}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.model, "gpt-6-luna")
        self.assertEqual(session.effort, "medium")
        self.assertEqual(session.total_token_usage["total_tokens"], 125)
        self.assertEqual(session.rate_limits["plan_type"], "plus")

    def test_malformed_lines_skipped(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-27T14:12:50.597Z"),
            "not json at all",
            json.dumps({"timestamp": "x", "type": "weird_type", "payload": {}}),
            turn_context_line("gpt-6-luna", "medium"),
            token_count_line({"input_tokens": 1, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 1,
                               "reasoning_output_tokens": 0, "total_tokens": 2}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.total_token_usage["total_tokens"], 2)

    def test_info_null_keeps_previous_usage(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-27T14:12:50.597Z"),
            token_count_line({"input_tokens": 1, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 1,
                               "reasoning_output_tokens": 0, "total_tokens": 2}),
            token_count_line(None, info_null=True),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.total_token_usage["total_tokens"], 2)

    def test_old_format_sums_and_dedupes_by_response_id(self):
        """Old rollouts have only token_usage_record lines (no token_count
        event_msg at all): usage must be summed, de-duplicated by
        response_id (last one per id wins), with all component fields
        filled in."""
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-astra", "medium"),
            token_usage_record_line("resp_1", {
                "input_tokens": 100, "cached_input_tokens": 10,
                "cache_write_input_tokens": 0, "output_tokens": 20,
                "reasoning_output_tokens": 5, "total_tokens": 135}),
            # Same response_id logged twice (e.g. retried); last one wins,
            # so it must not be double-counted.
            token_usage_record_line("resp_1", {
                "input_tokens": 100, "cached_input_tokens": 10,
                "cache_write_input_tokens": 0, "output_tokens": 20,
                "reasoning_output_tokens": 5, "total_tokens": 135}),
            token_usage_record_line("resp_2", {
                "input_tokens": 50, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 10,
                "reasoning_output_tokens": 2, "total_tokens": 62}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.model, "gpt-6-astra")
        usage = session.total_token_usage
        self.assertIsNotNone(usage)
        self.assertEqual(usage["input_tokens"], 150)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertEqual(usage["reasoning_output_tokens"], 7)
        self.assertEqual(usage["total_tokens"], 197)
        self.assertEqual(session.model_attribution, "per_response")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.model_calls, {"gpt-6-astra": 2})

    def test_both_formats_uses_token_count_only(self):
        """A file with both token_usage_record lines and a usable token_count
        event must use the token_count total, never adding the two."""
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-astra", "medium"),
            token_usage_record_line("resp_1", {
                "input_tokens": 100, "cached_input_tokens": 10,
                "cache_write_input_tokens": 0, "output_tokens": 20,
                "reasoning_output_tokens": 5, "total_tokens": 135}),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 10,
                               "cache_write_input_tokens": 0, "output_tokens": 20,
                               "reasoning_output_tokens": 5, "total_tokens": 135}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.total_token_usage["total_tokens"], 135)

    def test_response_usage_is_attributed_to_model_for_its_turn(self):
        path = self.tmp / "rollout.jsonl"
        luna = {"input_tokens": 100, "cached_input_tokens": 10,
                "cache_write_input_tokens": 0, "output_tokens": 20,
                "reasoning_output_tokens": 5, "total_tokens": 130}
        sol = {"input_tokens": 50, "cached_input_tokens": 5,
               "cache_write_input_tokens": 2, "output_tokens": 10,
               "reasoning_output_tokens": 1, "total_tokens": 60}
        total = {key: luna[key] + sol[key] for key in luna}
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-luna"),
            token_usage_record_line("resp-luna", luna, turn_id="turn-luna"),
            turn_context_line("gpt-6-sol", "high", turn_id="turn-sol"),
            token_usage_record_line("resp-sol", sol, turn_id="turn-sol"),
            token_count_line(total),
        ]), encoding="utf-8")

        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.model_attribution, "per_response")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.model_calls, {"gpt-6-luna": 1, "gpt-6-sol": 1})
        self.assertEqual(session.model_usage["gpt-6-luna"]["total_tokens"], 130)
        self.assertEqual(session.model_usage["gpt-6-sol"]["total_tokens"], 60)
        report = token_stats.build_codex_report([session])
        rows = {row["model"]: row for row in report["by_model"]}
        self.assertEqual(report["calls"], 2)
        self.assertEqual(rows["gpt-6-luna"]["calls"], 1)
        self.assertEqual(rows["gpt-6-sol"]["calls"], 1)
        self.assertAlmostEqual(sum(row["calls_pct"] for row in rows.values()), 100.0)
        self.assertAlmostEqual(sum(row["total_pct"] for row in rows.values()), 100.0)
        self.assertTrue(all(row["est_usd"] is not None for row in rows.values()))
        self.assertAlmostEqual(sum(row["cost_pct"] for row in rows.values()), 100.0)

    def test_multiple_responses_in_one_turn_each_count_as_calls(self):
        path = self.tmp / "rollout.jsonl"
        first = {"input_tokens": 100, "cached_input_tokens": 10,
                 "cache_write_input_tokens": 0, "output_tokens": 20,
                 "reasoning_output_tokens": 5, "total_tokens": 130}
        second = {"input_tokens": 60, "cached_input_tokens": 5,
                  "cache_write_input_tokens": 0, "output_tokens": 10,
                  "reasoning_output_tokens": 2, "total_tokens": 72}
        total = {key: first[key] + second[key] for key in first}
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-1"),
            token_usage_record_line("resp-1", first, turn_id="turn-1"),
            token_usage_record_line("resp-2", second, turn_id="turn-1"),
            token_count_line(total),
        ]), encoding="utf-8")

        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.model_calls, {"gpt-6-luna": 2})
        self.assertEqual(session.model_usage["gpt-6-luna"]["total_tokens"], 202)

    def test_mixed_models_without_turn_link_are_unknown(self):
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-1"),
            turn_context_line("gpt-6-sol", "high", turn_id="turn-2"),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 10,
                               "cache_write_input_tokens": 0, "output_tokens": 20,
                               "reasoning_output_tokens": 5, "total_tokens": 130}),
        ]), encoding="utf-8")

        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.model_attribution, "unknown")
        self.assertIsNone(session.model_usage)
        self.assertIsNone(session.calls)

    def test_unlinked_response_or_mismatched_total_is_unknown(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 10,
                 "cache_write_input_tokens": 0, "output_tokens": 20,
                 "reasoning_output_tokens": 5, "total_tokens": 130}
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-1"),
            token_usage_record_line("resp-1", usage, turn_id="missing-turn"),
            token_count_line(usage),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.model_attribution, "unknown")
        self.assertIsNone(session.calls)
        self.assertIsNone(session.model_usage)

        # A correctly linked response whose sum disagrees with the authoritative
        # cumulative snapshot must also lose model attribution.
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-1"),
            token_usage_record_line("resp-1", usage, turn_id="turn-1"),
            token_count_line({**usage, "total_tokens": 131}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        self.assertEqual(session.model_attribution, "unknown")
        self.assertIsNone(session.model_usage)

    def test_unpriced_model_keeps_partial_priced_total_and_shares(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 10,
                 "cache_write_input_tokens": 0, "output_tokens": 20,
                 "reasoning_output_tokens": 5, "total_tokens": 130}
        known_usage = {"input_tokens": 200, "cached_input_tokens": 20,
                       "cache_write_input_tokens": 0, "output_tokens": 30,
                       "reasoning_output_tokens": 5, "total_tokens": 235}
        path = self.tmp / "rollout.jsonl"
        path.write_text("\n".join([
            session_meta_line("s1", "2026-09-25T10:48:00.000Z"),
            turn_context_line("custom-unpriced-model", "medium", turn_id="turn-1"),
            token_usage_record_line("resp-1", usage, turn_id="turn-1"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-2"),
            token_usage_record_line("resp-2", known_usage, turn_id="turn-2"),
            token_count_line({key: usage[key] + known_usage[key] for key in usage}),
        ]), encoding="utf-8")
        session = token_stats.parse_codex_rollout(path)
        report = token_stats.build_codex_report([session])
        rows = {row["model"]: row for row in report["by_model"]}
        self.assertEqual(rows["custom-unpriced-model"]["calls"], 1)
        self.assertIsNone(rows["custom-unpriced-model"]["est_usd"])
        self.assertIsNone(rows["custom-unpriced-model"]["cost_pct"])
        self.assertIsNotNone(rows["gpt-6-luna"]["est_usd"])
        self.assertEqual(rows["gpt-6-luna"]["cost_pct"], 100.0)
        expected = token_stats.estimate_codex_cost_usd(known_usage, "gpt-6-luna")
        self.assertEqual(report["est_total_usd"], round(expected, 4))
        self.assertEqual(report["cost_estimate_status"], "partial")
        self.assertEqual(report["cost_basis"], "partial_api_list_estimate")
        text = "\n".join(token_stats.format_codex_section(report, self.tmp))
        self.assertIn("Cost estimate is partial", text)


class TestCollectAndReport(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "sessions"

        make_rollout(self.root, 2026, 9, 27, "rollout-a-s1.jsonl", [
            session_meta_line("s1", "2026-09-27T10:00:00.000Z"),
            turn_context_line("gpt-6-luna", "medium"),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 10,
                               "cache_write_input_tokens": 0, "output_tokens": 20,
                               "reasoning_output_tokens": 5, "total_tokens": 135},
                              rate_limits={"primary": {"used_percent": 30.0,
                                                        "window_minutes": 300},
                                           "secondary": {"used_percent": 40.0,
                                                         "window_minutes": 10080},
                                           "plan_type": "plus"}),
        ])
        make_rollout(self.root, 2026, 9, 28, "rollout-b-s2.jsonl", [
            session_meta_line("s2", "2026-09-28T10:00:00.000Z"),
            turn_context_line("gpt-6-luna", "high"),
            token_count_line({"input_tokens": 50, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 10,
                               "reasoning_output_tokens": 2, "total_tokens": 62},
                              rate_limits={"primary": {"used_percent": 55.0,
                                                        "window_minutes": 300},
                                           "secondary": {"used_percent": 60.0,
                                                         "window_minutes": 10080},
                                           "plan_type": "plus"}),
        ])
        make_rollout(self.root, 2026, 1, 1, "rollout-old.jsonl", [
            session_meta_line("s0", "2026-01-01T00:00:00.000Z"),
            turn_context_line("gpt-5", "low"),
            token_count_line({"input_tokens": 1, "cached_input_tokens": 0,
                               "cache_write_input_tokens": 0, "output_tokens": 1,
                               "reasoning_output_tokens": 0, "total_tokens": 2}),
        ])

    def test_per_model_totals_and_newest_rate_limits(self):
        sessions = token_stats.collect_codex_sessions(self.root)
        self.assertEqual(len(sessions), 3)
        report = token_stats.build_codex_report(sessions)
        self.assertEqual(report["sessions"], 3)
        model_names = {row["model"] for row in report["by_model"]}
        self.assertEqual(model_names, {"gpt-6-luna", "gpt-5"})
        luna_row = next(r for r in report["by_model"] if r["model"] == "gpt-6-luna")
        self.assertEqual(luna_row["total"], 135 + 62)
        self.assertEqual(report["totals"]["total"], 135 + 62 + 2)
        # s2 (2026-09-28) is newer than s1 (2026-09-27).
        self.assertEqual(report["rate_limits"]["primary"]["used_percent"], 55.0)
        self.assertEqual(report["rate_limits"]["plan_type"], "plus")

    def test_since_filter_drops_older_session(self):
        sessions = token_stats.collect_codex_sessions(self.root, since=date(2026, 9, 1))
        self.assertEqual({s.session_id for s in sessions}, {"s1", "s2"})

    def test_missing_dir_gives_none_report(self):
        missing = self.tmp / "does-not-exist"
        sessions = token_stats.collect_codex_sessions(missing)
        self.assertEqual(sessions, [])
        self.assertIsNone(token_stats.build_codex_report(sessions))


class TestRunIncludesCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.projects.mkdir()
        self.codex_root = self.tmp / "codex-sessions"
        make_rollout(self.codex_root, 2026, 9, 27, "rollout-a-s1.jsonl", [
            session_meta_line("s1", "2026-09-27T10:00:00.000Z"),
            turn_context_line("gpt-6-luna", "medium"),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 10,
                               "cache_write_input_tokens": 0, "output_tokens": 20,
                               "reasoning_output_tokens": 5, "total_tokens": 135}),
        ])

    def test_json_has_codex_section_with_no_message_content(self):
        text = token_stats.run(self.projects, as_json=True, codex_dir=self.codex_root)
        self.assertNotIn("prompt", text.lower())
        parsed = json.loads(text)
        self.assertIsNotNone(parsed["codex"])
        self.assertEqual(parsed["codex"]["sessions"], 1)
        self.assertEqual(parsed["codex"]["totals"]["total"], 135)

    def test_missing_codex_dir_gives_null_json_and_text_note(self):
        missing = self.tmp / "does-not-exist"
        parsed = json.loads(
            token_stats.run(self.projects, as_json=True, codex_dir=missing))
        self.assertIsNone(parsed["codex"])
        text = token_stats.run(self.projects, as_json=False, codex_dir=missing)
        self.assertIn("Codex: no sessions found", text)


if __name__ == "__main__":
    unittest.main()
