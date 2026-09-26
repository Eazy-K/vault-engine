"""Tests for tools/claude-hooks/statusline.py (Claude Code statusLine command).

Runs the script as a real subprocess, feeding it JSON on stdin and checking
stdout. Standard library only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "claude-hooks" / "statusline.py"


def _run(payload) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                           capture_output=True, text=True)


class TestStatusLine(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
