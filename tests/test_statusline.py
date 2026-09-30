"""Tests for tools/claude-hooks/statusline.py (Claude Code statusLine command).

Runs the script as a real subprocess, feeding it JSON on stdin and checking
stdout. Standard library only. Never touches the real ~/.claude/settings.json:
the settings path is always overridden via the CLAUDE_STATUSLINE_SETTINGS
environment variable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "statusline.py"


def _run(payload, env_extra=None) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = dict(os.environ)
    # Always point at a nonexistent settings file unless the caller overrides
    # it, so tests never accidentally read the real ~/.claude/settings.json.
    env.setdefault("CLAUDE_STATUSLINE_SETTINGS", str(REPO_ROOT / "___no_such_settings___.json"))
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                           capture_output=True, text=True, encoding="utf-8", env=env)


class TestStatusLineCtx(unittest.TestCase):
    """Existing ctx-segment behaviour, unchanged."""

    def test_formats_usage_with_size_and_percentage(self):
        out = _run({
            "context_window": {
                "current_usage": {
                    "input_tokens": 40000,
                    "cache_creation_input_tokens": 1000,
                    "cache_read_input_tokens": 2000,
                    "output_tokens": 500,
                },
                "context_window_size": 200000,
                "used_percentage": 21.75,
            },
        })
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "ctx 44K/200K 22%")

    def test_no_size_or_percentage_still_prints_tokens(self):
        out = _run({
            "context_window": {
                "current_usage": {"input_tokens": 1000},
            },
        })
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "ctx 1K")

    def test_null_usage_before_first_call(self):
        out = _run({"context_window": {"current_usage": None, "used_percentage": None}})
        self.assertEqual(out.returncode, 0)
        self.assertIn("no data yet", out.stdout)

    def test_missing_context_window(self):
        out = _run({})
        self.assertEqual(out.returncode, 0)
        self.assertIn("no data yet", out.stdout)

    def test_garbage_input(self):
        out = _run("not json {{{")
        self.assertEqual(out.returncode, 0)
        self.assertIn("ctx", out.stdout)

    def test_non_object_json(self):
        out = _run("[1, 2, 3]")
        self.assertEqual(out.returncode, 0)
        self.assertIn("no data yet", out.stdout)


class TestStatusLineSegments(unittest.TestCase):
    def test_full_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _run({
                "model": {"id": "opus-5.5", "display_name": "Opus 5.5"},
                "effort": {"level": "medium"},
                "workspace": {"current_dir": tmp},
                "context_window": {
                    "current_usage": {"input_tokens": 80000},
                    "context_window_size": 1000000,
                    "used_percentage": 8.0,
                },
                "cost": {"total_cost_usd": 1.4234},
            })
            self.assertEqual(out.returncode, 0)
            line = out.stdout.strip()
            self.assertIn("Opus 5.5·med", line)
            self.assertIn("ctx", line)
            self.assertIn("$1.42", line)
            self.assertIn(" │ ", line)

    def test_missing_model_omits_segment(self):
        out = _run({
            "context_window": {
                "current_usage": {"input_tokens": 1000},
            },
            "cost": {"total_cost_usd": 0.5},
        })
        self.assertEqual(out.returncode, 0)
        line = out.stdout.strip()
        self.assertNotIn("·", line)
        self.assertIn("ctx 1K", line)
        self.assertIn("$0.50", line)

    def test_missing_cost_omits_segment(self):
        out = _run({
            "model": {"id": "sonnet-5", "display_name": "Sonnet 5"},
            "context_window": {
                "current_usage": {"input_tokens": 1000},
            },
        })
        self.assertEqual(out.returncode, 0)
        line = out.stdout.strip()
        self.assertIn("Sonnet 5", line)
        self.assertNotIn("$", line)

    def test_non_numeric_cost_omits_segment(self):
        out = _run({
            "model": {"id": "sonnet-5", "display_name": "Sonnet 5"},
            "cost": {"total_cost_usd": "n/a"},
        })
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("$", out.stdout)

    def test_effort_from_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"modelSettings": {"sonnet-5": {"effortLevel": "high"}}}, f)
            out = _run(
                {"model": {"id": "sonnet-5", "display_name": "Sonnet 5"}},
                env_extra={"CLAUDE_STATUSLINE_SETTINGS": settings_path},
            )
            self.assertEqual(out.returncode, 0)
            self.assertIn("Sonnet 5·high", out.stdout)

    def test_effort_from_top_level_settings_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"effortLevel": "xhigh"}, f)
            out = _run(
                {"model": {"id": "unknown-model", "display_name": "Mystery"}},
                env_extra={"CLAUDE_STATUSLINE_SETTINGS": settings_path},
            )
            self.assertEqual(out.returncode, 0)
            self.assertIn("Mystery·xhigh", out.stdout)

    def test_no_effort_shows_only_model_name(self):
        out = _run({"model": {"id": "sonnet-5", "display_name": "Sonnet 5"}})
        self.assertEqual(out.returncode, 0)
        line = out.stdout.strip()
        self.assertIn("Sonnet 5", line)
        self.assertNotIn("·", line)

    def test_effort_level_from_stdin_overrides_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = os.path.join(tmp, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump({"effortLevel": "low"}, f)
            out = _run(
                {
                    "model": {"id": "sonnet-5", "display_name": "Sonnet 5"},
                    "effort": {"level": "max"},
                },
                env_extra={"CLAUDE_STATUSLINE_SETTINGS": settings_path},
            )
            self.assertEqual(out.returncode, 0)
            self.assertIn("Sonnet 5·max", out.stdout)

    def test_non_git_dir_omits_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = _run({"workspace": {"current_dir": tmp}})
            self.assertEqual(out.returncode, 0)
            line = out.stdout.strip()
            self.assertIn(os.path.basename(tmp), line)
            folder_part = line.split(" │ ")[0]
            self.assertNotIn("(", folder_part)

    def test_falls_back_to_cwd_when_no_workspace(self):
        out = _run({"cwd": str(REPO_ROOT)})
        self.assertEqual(out.returncode, 0)
        self.assertIn(REPO_ROOT.name, out.stdout)


def _assistant(msg_id, model, out_tokens, inp=0):
    return json.dumps({
        "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
        "message": {"id": msg_id, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": out_tokens}},
    }) + "\n"


class TestModelShares(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.proj = root / "proj"
        (self.proj / "sess1" / "subagents").mkdir(parents=True)
        self.main = self.proj / "sess1.jsonl"
        self.sub = self.proj / "sess1" / "subagents" / "agent-a1.jsonl"
        self.cache = root / "cache"
        self.env = {"CLAUDE_STATUSLINE_CACHE": str(self.cache)}

    def _line(self, transcript=None):
        out = _run({"session_id": "sess1",
                    "transcript_path": str(transcript or self.main)}, self.env)
        self.assertEqual(out.returncode, 0)
        return out.stdout.strip()

    def test_shares_combine_main_and_subagents(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 600)
                             + _assistant("m2", "claude-opus-5-5", 50), encoding="utf-8")
        self.sub.write_text(_assistant("s1", "claude-sonnet-5-5", 300)
                            + _assistant("s2", "claude-fable-1", 50), encoding="utf-8")
        self.assertIn("Opus 65%·Sonnet 30%·Fable 5%", self._line())

    def test_share_under_one_percent_omitted(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 10000)
                             + _assistant("m2", "claude-haiku-5", 10), encoding="utf-8")
        self.assertNotIn("Opus", self._line())

    def test_single_model_no_segment(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 100), encoding="utf-8")
        self.sub.write_text(_assistant("s1", "claude-opus-5-5", 100), encoding="utf-8")
        self.assertNotIn("%", self._line())

    def test_missing_transcript_path(self):
        out = _run({"session_id": "sess1"}, self.env)
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("Opus", out.stdout)
        out = _run({"transcript_path": str(self.proj / "nope.jsonl")}, self.env)
        self.assertEqual(out.returncode, 0)

    def test_incremental_append_and_partial_line(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 100)
                             + _assistant("m2", "claude-sonnet-5-5", 100), encoding="utf-8")
        self.assertIn("Opus 50%·Sonnet 50%", self._line())
        partial = _assistant("m3", "claude-sonnet-5-5", 200)
        with open(self.main, "a", encoding="utf-8", newline="") as f:
            f.write(partial[:-1])  # no trailing newline yet: must not count
        self.assertIn("Opus 50%·Sonnet 50%", self._line())
        with open(self.main, "a", encoding="utf-8", newline="") as f:
            f.write("\n")
        self.assertIn("Sonnet 75%·Opus 25%", self._line())
        # The cache now sits at the end of the file: only new bytes are read.
        cached = json.loads(next(self.cache.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(cached["files"][str(self.main)]["off"], self.main.stat().st_size)

    def test_dedup_by_message_id_last_wins(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 10)
                             + _assistant("m1", "claude-opus-5-5", 100)
                             + _assistant("m2", "claude-sonnet-5-5", 100), encoding="utf-8")
        self.assertIn("Opus 50%·Sonnet 50%", self._line())
        with open(self.main, "a", encoding="utf-8") as f:
            f.write(_assistant("m1", "claude-opus-5-5", 300))  # updated in a later run
        self.assertIn("Opus 75%·Sonnet 25%", self._line())

    def test_corrupted_cache_falls_back(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 100)
                             + _assistant("m2", "claude-sonnet-5-5", 100), encoding="utf-8")
        self._line()
        for f in self.cache.glob("*.json"):
            f.write_text("{not json", encoding="utf-8")
        self.assertIn("Opus 50%·Sonnet 50%", self._line())
        for f in self.cache.glob("*.json"):
            f.write_text(json.dumps({"v": 1, "files": {str(self.main): {"off": "x"}}}),
                         encoding="utf-8")
        self.assertIn("Opus 50%·Sonnet 50%", self._line())

    def test_shrunk_file_reparsed(self):
        self.main.write_text(_assistant("m1", "claude-opus-5-5", 100)
                             + _assistant("m2", "claude-sonnet-5-5", 100)
                             + _assistant("m3", "claude-sonnet-5-5", 100), encoding="utf-8")
        self.assertIn("Sonnet 67%·Opus 33%", self._line())
        self.main.write_text(_assistant("m9", "claude-opus-5-5", 300)
                             + _assistant("m8", "claude-sonnet-5-5", 100), encoding="utf-8")
        self.assertIn("Opus 75%·Sonnet 25%", self._line())

    def test_matches_token_stats_load_calls(self):
        import importlib.util
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        try:
            import token_stats
        finally:
            sys.path.pop(0)
        spec = importlib.util.spec_from_file_location("statusline_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        lines = (_assistant("a", "claude-opus-5-5", 10, inp=5)
                 + _assistant("a", "claude-opus-5-5", 40, inp=5)
                 + _assistant("b", "claude-sonnet-5-5", 7)
                 + _assistant("c", "<synthetic>", 999)
                 + "garbage\n" + json.dumps({"type": "user"}) + "\n")
        self.main.write_text(lines, encoding="utf-8")
        expected: dict[str, int] = {}
        for c in token_stats.load_calls(self.main):
            expected[c.model] = expected.get(c.model, 0) + c.total
        os.environ["CLAUDE_STATUSLINE_CACHE"] = str(self.cache)
        self.addCleanup(os.environ.pop, "CLAUDE_STATUSLINE_CACHE", None)
        got = mod.session_model_totals({"session_id": "sess1",
                                        "transcript_path": str(self.main)})
        self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
