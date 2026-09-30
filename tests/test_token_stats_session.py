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
    make_rollout, session_meta_line, token_count_line, turn_context_line)

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
        self.assertIn("gpt-6-luna", self.session(CODEX_SID))

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
