"""tools/claude-agents/worker-design*.md: the Claude-only UI / visual design roles
(task 0066). Checks the frontmatter shape against the existing worker files and the
rules each prompt must carry."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: E402,F401

AGENTS = Path(__file__).resolve().parent.parent / "tools" / "claude-agents"
KEYS = ["name", "description", "model", "effort", "omitClaudeMd", "tools", "maxTurns"]


def _split(name: str) -> tuple[dict[str, str], list[str], str]:
    text = (AGENTS / name).read_text(encoding="utf-8")
    _, head, body = text.split("---\n", 2)
    order, values = [], {}
    for line in head.splitlines():
        key, _, value = line.partition(":")
        order.append(key)
        values[key] = value.strip()
    return values, order, body


class TestWorkerDesignAgent(unittest.TestCase):
    def test_frontmatter(self):
        fm, order, _ = _split("worker-design.md")
        self.assertEqual(order, KEYS)
        self.assertEqual(fm["name"], "worker-design")
        self.assertEqual((fm["model"], fm["effort"], fm["maxTurns"]), ("opus", "high", "90"))
        self.assertEqual(fm["omitClaudeMd"], "true")
        self.assertEqual(fm["tools"], _split("worker-medium.md")[0]["tools"])
        self.assertTrue(fm["description"].endswith("Do not pick this agent on your own."))

    def test_keeps_every_worker_medium_rule(self):
        _, _, medium = _split("worker-medium.md")
        _, _, design = _split("worker-design.md")
        for line in medium.splitlines():
            if line.strip():
                self.assertIn(line, design)

    def test_design_rules(self):
        _, _, body = _split("worker-design.md")
        for phrase in ("## Screenshots", "## Ekran görüntüleri", "design language",
                       "stop and ask", "PNG", "`Read`", "blocker", "design decisions",
                       "known visual gaps", "Software design"):
            self.assertIn(phrase, body, phrase)


class TestWorkerDesignVerifyAgent(unittest.TestCase):
    def test_frontmatter(self):
        fm, order, _ = _split("worker-design-verify.md")
        self.assertEqual(order, KEYS)
        self.assertEqual(fm["name"], "worker-design-verify")
        self.assertEqual((fm["model"], fm["effort"], fm["maxTurns"]), ("opus", "medium", "38"))
        self.assertEqual(fm["omitClaudeMd"], "true")
        self.assertEqual(fm["tools"], _split("worker-verify.md")[0]["tools"])
        self.assertNotIn("Edit", fm["tools"])
        self.assertNotIn("Write", fm["tools"])
        self.assertTrue(fm["description"].endswith("Do not pick this agent on your own."))

    def test_checklist_and_output_format(self):
        _, _, body = _split("worker-design-verify.md")
        for item in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)"):
            self.assertIn(f"- {item} ", body, item)
        for phrase in ("First line exactly `PASS` or `FAIL`.", "`- ok: ...` or `- fail: ...`",
                       "at most 5 lines", "screenshot", "PNG", "`Read`",
                       "## Screenshots", "## Ekran görüntüleri", "taste",
                       "never fix", "Read-only"):
            self.assertIn(phrase, body, phrase)

    def test_missing_screenshots_fail(self):
        _, _, body = _split("worker-design-verify.md")
        self.assertIn("If the screenshot command or the PNGs are missing, answer `FAIL`", body)


if __name__ == "__main__":
    unittest.main()
