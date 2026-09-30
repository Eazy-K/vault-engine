"""Tests for the model-share columns and the --session view of tools/token_stats.py.

Uses only tempfile-based fake transcript/rollout trees and fake session ids.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_token_stats import assistant_line, token_stats  # noqa: E402
from test_token_stats_codex import (  # noqa: E402
    make_rollout, session_meta_line, token_count_line,
    token_usage_record_line, turn_context_line)

SID = "11111111-aaaa-bbbb-cccc-000000000001"
CODEX_SID = "22222222-aaaa-bbbb-cccc-000000000002"
OPUS, SONNET = "claude-opus-5", "claude-sonnet-5"
T0, T1 = "2026-09-01T10:00:00.000Z", "2026-09-01T10:30:00.000Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = self.tmp / "projects"
        self.codex = self.tmp / "codex"
        self.cwd = self.tmp / "work"
        self.slug = token_stats.project_slug_for(self.cwd)
        self.proj_dir = self.projects / self.slug
        self.proj_dir.mkdir(parents=True)

    def write_session(self, sid=SID):
        (self.proj_dir / f"{sid}.jsonl").write_text("\n".join([
            assistant_line("m1", OPUS, T0, input_t=100, output_t=100),
            assistant_line("m2", OPUS, T1, input_t=100, output_t=100),
        ]), encoding="utf-8")
        sub = self.proj_dir / sid / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.jsonl").write_text(
            assistant_line("s1", SONNET, T1, input_t=300, output_t=200), encoding="utf-8")
        (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "explorer"}),
                                                 encoding="utf-8")

    def write_codex(self, sid=CODEX_SID):
        return make_rollout(self.codex, 2026, 9, 27, f"rollout-2026-09-27T14-12-50-{sid}.jsonl", [
            session_meta_line(sid, "2026-09-27T14:12:50.597Z"),
            turn_context_line("gpt-6-luna", "medium"),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 40,
                               "output_tokens": 20, "reasoning_output_tokens": 5,
                               "total_tokens": 120}),
        ])

    def write_codex_child(self, sid, parent_sid):
        usage = {"input_tokens": 80, "cached_input_tokens": 10,
                 "cache_write_input_tokens": 0, "output_tokens": 20,
                 "reasoning_output_tokens": 5, "total_tokens": 105}
        return make_rollout(self.codex, 2026, 9, 27,
                            f"rollout-2026-09-27T14-13-50-{sid}.jsonl", [
            session_meta_line(sid, "2026-09-27T14:13:50.597Z",
                              parent_thread_id=parent_sid, agent_role="worker"),
            turn_context_line("gpt-6-sol", "medium", turn_id="child-turn"),
            token_usage_record_line("child-response", usage,
                                    turn_id="child-turn", thread_id=sid),
            token_count_line(usage),
        ])

    def write_linked_codex_parent(self, sid=CODEX_SID):
        usage = {"input_tokens": 100, "cached_input_tokens": 20,
                 "cache_write_input_tokens": 0, "output_tokens": 30,
                 "reasoning_output_tokens": 5, "total_tokens": 130}
        return make_rollout(self.codex, 2026, 9, 27,
                            f"rollout-2026-09-27T14-12-50-{sid}.jsonl", [
            session_meta_line(sid, "2026-09-27T14:12:50.597Z",
                              parent_thread_id=None),
            turn_context_line("gpt-6-luna", "medium", turn_id="parent-turn"),
            token_usage_record_line("parent-response", usage,
                                    turn_id="parent-turn", thread_id=sid),
            token_count_line(usage),
        ])

    def session(self, spec, env=None, as_json=False):
        return token_stats.run_session(self.projects, self.codex, spec, cwd=self.cwd,
                                       env=env or {}, as_json=as_json)


class TestShares(Base):
    def test_overall_shares_split_and_text(self):
        self.write_session()
        sessions = token_stats.collect_sessions(self.projects)
        report = token_stats.build_report(sessions)
        rows = {r["model"]: r for r in report["by_model"]}
        for key in ("calls_pct", "total_pct", "cost_pct"):
            self.assertAlmostEqual(sum(r[key] for r in rows.values()), 100.0, delta=0.2)
        self.assertEqual((rows[OPUS]["main_calls"], rows[OPUS]["sub_calls"]), (2, 0))
        self.assertEqual((rows[SONNET]["main_calls"], rows[SONNET]["sub_calls"]), (0, 1))
        text = token_stats.format_report_text(report, self.projects, None)
        self.assertIn("main 2 / sub 0", text)
        self.assertIn("66.7%", text)  # opus call share

    def test_codex_shares(self):
        self.write_codex()
        report = token_stats.build_codex_report(token_stats.collect_codex_sessions(self.codex))
        row = report["by_model"][0]
        self.assertEqual((row["sessions_pct"], row["total_pct"]), (100.0, 100.0))
        self.assertIn("100.0%", "\n".join(token_stats.format_codex_section(report, self.codex)))

    def test_zero_totals_show_dash(self):
        self.assertIsNone(token_stats.pct(0, 0))
        self.assertEqual(token_stats.fmt_pct(None), "-")
        (self.proj_dir / f"{SID}.jsonl").write_text(
            assistant_line("m1", OPUS, T0), encoding="utf-8")
        out = self.session(SID)
        self.assertIn("-", out)
        data = json.loads(self.session(SID, as_json=True))
        self.assertIsNone(data["by_model"][0]["total_pct"])
        self.assertIsNone(data["main"]["tokens_pct"])


class TestSession(Base):
    def test_claude_id_with_subagents(self):
        self.write_session()
        data = json.loads(self.session(SID, as_json=True))
        self.assertEqual(data["kind"], "claude")
        self.assertEqual(data["duration_seconds"], 1800)
        self.assertEqual(data["total_tokens"], 900)
        self.assertEqual(data["main"]["total_tokens"], 400)
        self.assertEqual(data["subagents_total"]["total_tokens"], 500)
        self.assertEqual(len(data["subagents"]), 1)
        sub = data["subagents"][0]
        self.assertEqual((sub["agent_type"], sub["model"], sub["calls"]), ("explorer", SONNET, 1))
        self.assertEqual(sub["tokens_pct"], 55.6)
        text = self.session(SID)
        self.assertIn("explorer", text)
        self.assertIn("55.6%", text)
        self.assertIn("est. total*", text)

    def test_codex_id(self):
        self.write_codex()
        data = json.loads(self.session(CODEX_SID, as_json=True))
        self.assertEqual(data["kind"], "codex")
        self.assertEqual(data["models"][0]["total"], 120)
        self.assertEqual(data["models"][0]["cached_input"], 40)
        self.assertEqual(data["model_attribution"], "single_model_session")
        self.assertIsNone(data["calls"])
        self.assertIsNone(data["models"][0]["calls"])
        self.assertIsNone(data["models"][0]["calls_pct"])
        self.assertEqual(data["agent_breakdown"]["status"], "unknown")
        text = self.session(CODEX_SID)
        self.assertEqual(text, token_stats.format_session_text(data))
        self.assertIn("gpt-6-luna", text)

    def test_codex_session_aggregates_verified_parent_and_child(self):
        self.write_linked_codex_parent()
        self.write_codex_child("child-session", CODEX_SID)

        data = json.loads(self.session(CODEX_SID, as_json=True))
        self.assertEqual(data["agent_breakdown"]["status"], "verified")
        self.assertEqual(data["totals"]["total"], 235)
        self.assertEqual(data["totals"]["input"], 180)
        self.assertEqual(data["totals"]["cached_input"], 30)
        self.assertEqual(data["main_totals"]["input"], 100)
        self.assertEqual(data["main_totals"]["total"], 130)
        models = {row["model"]: row for row in data["models"]}
        self.assertEqual(set(models), {"gpt-6-luna", "gpt-6-sol"})
        self.assertEqual(models["gpt-6-luna"]["total"], 130)
        self.assertEqual(models["gpt-6-sol"]["total"], 105)
        self.assertEqual(models["gpt-6-luna"]["calls"], 1)
        self.assertEqual(models["gpt-6-sol"]["calls"], 1)
        self.assertAlmostEqual(sum(row["total_pct"] for row in models.values()), 100.0)
        expected_cost = (
            token_stats.estimate_codex_cost_usd(
                {"input_tokens": 100, "cached_input_tokens": 20,
                 "output_tokens": 30, "total_tokens": 130}, "gpt-6-luna")
            + token_stats.estimate_codex_cost_usd(
                {"input_tokens": 80, "cached_input_tokens": 10,
                 "output_tokens": 20, "total_tokens": 105}, "gpt-6-sol"))
        self.assertAlmostEqual(data["est_total_usd"], round(expected_cost, 4))
        # Per-model rows and the total are rounded independently to 4 decimals.
        self.assertLessEqual(round(abs(sum(row["est_usd"] for row in models.values())
                                       - data["est_total_usd"]), 7), 0.0001)
        self.assertEqual(models["gpt-6-luna"]["main_tokens"], 130)
        self.assertEqual(models["gpt-6-sol"]["subagent_tokens"], 105)
        self.assertEqual(data["main"]["total_tokens"], 130)
        self.assertEqual(data["subagents_total"]["total_tokens"], 105)
        self.assertEqual(data["subagents_total"]["runs"], 1)
        self.assertEqual(len(data["subagents"]), 1)
        child = data["subagents"][0]
        self.assertEqual(child["session_id"], "child-session")
        self.assertEqual(child["agent_role"], "worker")
        self.assertEqual(child["calls"], 1)
        self.assertEqual(child["total_tokens"], 105)
        self.assertAlmostEqual(data["main"]["tokens_pct"] +
                               data["subagents_total"]["tokens_pct"], 100.0)

        text = self.session(CODEX_SID)
        self.assertEqual(text, token_stats.format_session_text(data))
        self.assertIn("gpt-6-luna", text)
        self.assertIn("gpt-6-sol", text)
        self.assertIn("child-session", text)
        self.assertIn("105", text)

    def test_codex_session_partial_cost_keeps_priced_subtotal(self):
        self.write_linked_codex_parent()
        unknown = {"input_tokens": 50, "cached_input_tokens": 5,
                   "cache_write_input_tokens": 0, "output_tokens": 10,
                   "reasoning_output_tokens": 2, "total_tokens": 62}
        make_rollout(self.codex, 2026, 9, 27,
                     "rollout-2026-09-27T14-13-50-child-session.jsonl", [
            session_meta_line("child-session", "2026-09-27T14:13:50.597Z",
                              parent_thread_id=CODEX_SID, agent_role="worker"),
            turn_context_line("custom-unpriced-model", "medium", turn_id="child-turn"),
            token_usage_record_line("child-response", unknown,
                                    turn_id="child-turn", thread_id="child-session"),
            token_count_line(unknown),
        ])

        data = json.loads(self.session(CODEX_SID, as_json=True))
        models = {row["model"]: row for row in data["models"]}
        self.assertIsNone(models["custom-unpriced-model"]["est_usd"])
        self.assertIsNone(models["custom-unpriced-model"]["cost_pct"])
        self.assertEqual(models["gpt-6-luna"]["cost_pct"], 100.0)
        expected = token_stats.estimate_codex_cost_usd(
            {"input_tokens": 100, "cached_input_tokens": 20,
             "cache_write_input_tokens": 0, "output_tokens": 30}, "gpt-6-luna")
        self.assertEqual(data["est_total_usd"], round(expected, 4))
        self.assertEqual(data["cost_estimate_status"], "partial")
        text = self.session(CODEX_SID)
        self.assertIn("partial; priced models only", text)

    def test_codex_unlinked_and_unattributed_usage_stays_unknown(self):
        make_rollout(self.codex, 2026, 9, 27, f"rollout-mixed-{CODEX_SID}.jsonl", [
            session_meta_line(CODEX_SID, "2026-09-27T14:12:50.597Z"),
            turn_context_line("gpt-6-luna", "medium", turn_id="turn-a"),
            turn_context_line("gpt-6-sol", "high", turn_id="turn-b"),
            token_count_line({"input_tokens": 100, "cached_input_tokens": 20,
                               "cache_write_input_tokens": 0, "output_tokens": 30,
                               "reasoning_output_tokens": 5, "total_tokens": 130}),
        ])

        data = json.loads(self.session(CODEX_SID, as_json=True))
        self.assertEqual(data["model_attribution"], "partial_or_unknown")
        self.assertEqual(data["agent_breakdown"]["status"], "unknown")
        self.assertEqual(data["models"][0]["model"], "unknown")
        self.assertIsNone(data["models"][0]["calls"])
        self.assertIsNone(data["models"][0]["calls_pct"])
        text = self.session(CODEX_SID)
        self.assertIn("unknown", text.lower())

    def test_current_via_claude_env(self):
        self.write_session()
        text = self.session("current", env={"CLAUDE_CODE_SESSION_ID": SID})
        self.assertIn(SID, text)
        self.assertNotIn("a guess", text)

    def test_current_via_codex_env(self):
        self.write_codex()
        text = self.session("current", env={"CODEX_SESSION_ID": CODEX_SID})
        self.assertIn("Codex session " + CODEX_SID, text)
        text = self.session("current", env={"CODEX_THREAD_ID": CODEX_SID})
        self.assertIn("Codex session " + CODEX_SID, text)

    def test_current_fallback_guess_note(self):
        self.write_session()
        other = self.projects / "other-project"
        other.mkdir()
        (other / "99999999-x.jsonl").write_text(
            assistant_line("o1", OPUS, T0, input_t=1), encoding="utf-8")
        text = self.session("current")
        self.assertTrue(text.startswith(token_stats.NO_ENV_NOTE))
        self.assertIn(SID, text)  # cwd's project wins over the other one
        data = json.loads(self.session("current", as_json=True))
        self.assertEqual(data["note"], token_stats.NO_ENV_NOTE)

    def test_unknown_id_errors(self):
        self.write_session()
        with self.assertRaises(token_stats.SessionNotFound):
            self.session("does-not-exist")
        with self.assertRaises(token_stats.SessionNotFound):
            token_stats.run_session(self.tmp / "none", self.tmp / "none", "current",
                                    cwd=self.cwd, env={})


if __name__ == "__main__":
    unittest.main()
