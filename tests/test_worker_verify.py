"""Tests for tools/verify_task.py (`graph.py verify`, the mechanical part of worker-verify)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph  # noqa: E402

sys.path.insert(0, str(GRAPH_PATH.parent))
import verify_task  # noqa: E402

verify_task.g = graph  # other test modules replace sys.modules["graph"]; bind to ours

TASK = """---
type: task
status: in-progress
---
Do the thing.

## Scope
- Hariç: `docs/legacy.md`

## Files not to touch
- `CHANGELOG.md`
- `tools/codex-agents/`
- `tools/*.toml`

## Acceptance
```
python -I -m unittest discover -s tests
```
"""

TASK_HUMAN = """---
type: task
status: in-progress
---
## Kabul
```
human
```

## Dokunulmayacak dosyalar
- `README.md`
"""


def diff_for(*paths: str) -> str:
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n" for p in paths)


GREEN = "Ran 12 tests in 0.1s\n\nOK\n"


def body_of(text: str) -> str:
    return graph.parse_frontmatter(text)[1]


class TestHelpers(unittest.TestCase):
    def test_excluded_paths_english_heading_and_exclude_bullet(self):
        found = verify_task.excluded_paths(body_of(TASK))
        self.assertEqual(found, ["docs/legacy.md", "CHANGELOG.md",
                                 "tools/codex-agents/", "tools/*.toml"])

    def test_excluded_paths_turkish_heading(self):
        body = "## Dokunulmayacak dosyalar\n- `a\\b.py`, `./c.md`\n\n## Sonra\n- `not/me.txt`\n"
        self.assertEqual(verify_task.excluded_paths(body), ["a/b.py", "c.md"])

    def test_excluded_paths_ignores_prose_words(self):
        body = "## Files not to touch\nnothing else, only these: x.py\n"
        self.assertEqual(verify_task.excluded_paths(body), ["x.py"])

    def test_diff_paths_prefers_git_headers(self):
        self.assertEqual(verify_task.diff_paths(diff_for("a/b.py", "c.md")), ["a/b.py", "c.md"])

    def test_diff_paths_plus_plus_fallback_and_dev_null(self):
        text = "--- a/x.py\n+++ b/x.py\n@@\n+1\n--- a/y.py\n+++ /dev/null\n"
        self.assertEqual(verify_task.diff_paths(text), ["x.py"])

    def test_acceptance_green(self):
        self.assertTrue(verify_task.acceptance_green(GREEN)[0])
        self.assertTrue(verify_task.acceptance_green("something\nexit=0\n")[0])
        self.assertFalse(verify_task.acceptance_green("Ran 3 tests\n\nFAILED (failures=1)\n")[0])
        self.assertFalse(verify_task.acceptance_green("OK\nexit code: 1\n")[0])
        self.assertFalse(verify_task.acceptance_green("")[0])

    def test_glob_directory_and_suffix_matching(self):
        m = verify_task._matches
        self.assertTrue(m("tools/*.toml", "tools/x.toml"))
        self.assertTrue(m("tools/codex-agents/", "tools/codex-agents/a.toml"))
        self.assertFalse(m("tools/codex-agents/", "tools/claude-agents/a.md"))
        self.assertTrue(m("CHANGELOG.md", "CHANGELOG.md"))
        self.assertTrue(m("CHANGELOG.md", "sub/CHANGELOG.md"))
        self.assertFalse(m("CHANGELOG.md", "NOTCHANGELOG.md"))


class TestEvaluate(unittest.TestCase):
    def test_clean_diff_and_green_output_passes(self):
        ok, items = verify_task.evaluate(body_of(TASK), diff_for("tools/a.py"), GREEN)
        self.assertTrue(ok)
        self.assertTrue(all(i.startswith("- ok:") for i in items), items)

    def test_excluded_file_fails_and_is_named(self):
        ok, items = verify_task.evaluate(body_of(TASK), diff_for("tools/a.py", "CHANGELOG.md",
                                                                 "tools/codex-agents/w.toml"), GREEN)
        self.assertFalse(ok)
        line = [i for i in items if i.startswith("- fail:")][0]
        self.assertIn("CHANGELOG.md", line)
        self.assertIn("tools/codex-agents/w.toml", line)

    def test_hari_bullet_is_enforced(self):
        ok, items = verify_task.evaluate(body_of(TASK), diff_for("docs/legacy.md"), GREEN)
        self.assertFalse(ok)

    def test_missing_acceptance_output_fails(self):
        ok, items = verify_task.evaluate(body_of(TASK), diff_for("tools/a.py"), None)
        self.assertFalse(ok)
        self.assertTrue(any("acceptance output missing" in i for i in items))

    def test_red_acceptance_output_fails(self):
        ok, _ = verify_task.evaluate(body_of(TASK), diff_for("tools/a.py"), "FAILED (errors=1)\n")
        self.assertFalse(ok)

    def test_human_acceptance_is_ok_without_output(self):
        ok, items = verify_task.evaluate(body_of(TASK_HUMAN), diff_for("tools/a.py"), None)
        self.assertTrue(ok)
        self.assertIn("acceptance: human / none", items[0])
        ok, _ = verify_task.evaluate(body_of(TASK_HUMAN), diff_for("README.md"), None)
        self.assertFalse(ok)

    def test_empty_diff_fails(self):
        ok, items = verify_task.evaluate(body_of(TASK), "", GREEN)
        self.assertFalse(ok)
        self.assertTrue(any("empty diff" in i for i in items))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.task = self.tmp / "0001-task.md"
        self.task.write_text(TASK, encoding="utf-8", newline="\n")
        self.out = self.tmp / "acceptance.txt"
        self.out.write_text(GREEN, encoding="utf-8", newline="\n")

    def run_verify(self, diff_text: str, with_output: bool = True) -> subprocess.CompletedProcess:
        diff = self.tmp / "change.diff"
        diff.write_text(diff_text, encoding="utf-8", newline="\n")
        cmd = [sys.executable, "-I", str(GRAPH_PATH), "verify", "--task", str(self.task),
               "--diff", str(diff)]
        if with_output:
            cmd += ["--acceptance-output", str(self.out)]
        env = {k: v for k, v in os.environ.items() if k not in ("VAULT_DATA", "VAULT_HOME")}
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_cli_pass(self):
        res = self.run_verify(diff_for("tools/a.py"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0], "PASS")

    def test_cli_fail_names_excluded_file(self):
        res = self.run_verify(diff_for("CHANGELOG.md"))
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(res.stdout.splitlines()[0], "FAIL")
        self.assertIn("CHANGELOG.md", res.stdout)

    def test_cli_missing_acceptance_output_fails(self):
        res = self.run_verify(diff_for("tools/a.py"), with_output=False)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("acceptance output missing", res.stdout)


if __name__ == "__main__":
    unittest.main()
