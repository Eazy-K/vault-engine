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


if __name__ == "__main__":
    unittest.main()
