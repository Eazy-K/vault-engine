"""Tests for tools/hooks/_find-python.sh (shared by pre-commit and commit-msg).

The bug this guards against: `command -v python` (or `python3`) only checks
that a name resolves on PATH, which the Windows Store's python.exe stub
satisfies without ever running real Python -- so every candidate must be
executed (`-c "import sys"`) before it is trusted. POSIX shell scripts only;
skipped where no `sh` is available to run them (matches how git itself runs
hooks, regardless of the OS).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "hooks" / "_find-python.sh"

SH = shutil.which("sh") or shutil.which("bash")


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


@unittest.skipUnless(SH, "no POSIX shell available to run the hook scripts")
class TestFindPython(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.driver = self.tmp / "drive.sh"
        self.driver.write_text(
            f'#!/bin/sh\n. "{SCRIPT.as_posix()}"\necho "RESULT:$PYTHON_CMD:$PYTHON_ARGS"\n',
            encoding="utf-8", newline="\n")
        self.driver.chmod(0o755)

    def _run(self, bin_names: dict[str, str]) -> subprocess.CompletedProcess:
        """bin_names: {command name: shell body}. Runs the driver script with
        only these stubs on PATH (plus SH itself, so it can still find `sh`)."""
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        for name, body in bin_names.items():
            _write_stub(bindir / name, body)
        sh_dir = str(Path(SH).parent)
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(bindir), sh_dir])
        return subprocess.run([SH, str(self.driver)], capture_output=True, text=True, env=env)

    def _result(self, out: subprocess.CompletedProcess) -> str:
        for line in out.stdout.splitlines():
            if line.startswith("RESULT:"):
                return line[len("RESULT:"):]
        return f"<no RESULT line> stdout={out.stdout!r} stderr={out.stderr!r}"

    def test_prefers_py_dash_3_when_it_actually_runs(self):
        out = self._run({
            "py": 'if [ "$1" = -3 ] && [ "$2" = -c ]; then exit 0; fi; exit 1',
            "python3": "exit 0",
            "python": "exit 0",
        })
        self.assertEqual(self._result(out), "py:-3")

    def test_skips_a_python_that_resolves_but_fails_to_run(self):
        # The Windows Store stub: `command -v python` finds it, but running
        # it (e.g. `-c "import sys"`) fails.
        out = self._run({"python": "exit 1", "python3": "exit 0"})
        self.assertEqual(self._result(out), "python3:")

    def test_falls_back_to_plain_python_last(self):
        out = self._run({"python": "exit 0"})
        self.assertEqual(self._result(out), "python:")

    def test_no_working_interpreter_is_an_error(self):
        out = self._run({"python": "exit 1", "python3": "exit 1", "py": "exit 1"})
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("python not found", out.stderr)


if __name__ == "__main__":
    unittest.main()
