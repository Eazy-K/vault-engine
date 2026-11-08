"""doctor's Codex checks: CLI version / login, sandbox, effort, worker profiles, skill,
git-guard hook. Temporary CODEX_HOME / Claude dir and a mocked codex CLI throughout."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
spec = importlib.util.spec_from_file_location("graph", TOOLS_DIR / "graph.py")
graph = importlib.util.module_from_spec(spec)
sys.modules["graph"] = graph
spec.loader.exec_module(graph)
sys.path.insert(0, str(TOOLS_DIR))
import codex_hooks  # noqa: E402
import onboarding  # noqa: E402


def _run(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestDoctorCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "codex"
        (self.home / "agents").mkdir(parents=True)
        self.claude = self.tmp / "claude"

    def _install_all(self):
        for src in codex_hooks.AGENTS_DIR.glob("worker-*.toml"):
            shutil.copy(src, self.home / "agents" / src.name)
        hooks, _ = codex_hooks.merge({})
        (self.home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        skill = onboarding._skill_sources()[0]
        dest = self.claude / "skills" / skill.parent.name / "SKILL.md"
        dest.parent.mkdir(parents=True)
        shutil.copy(skill, dest)

    def _all(self):
        lines = [codex_hooks.hooks_status(self.home / "hooks.json"),
                 codex_hooks.workers_status(self.home / "agents")]
        lines += codex_hooks.cli_statuses("codex", self.home / "config.toml", self.home)
        stale = onboarding.stale_claude_skills(self.claude)
        lines.append(("WARN", f"skills {stale}") if stale else ("OK", "skills"))
        return lines

    def test_all_present_is_ok(self):
        self._install_all()
        (self.home / "config.toml").write_text(
            'model_reasoning_effort = "medium"\n[windows]\nsandbox = "unelevated"\n', encoding="utf-8")
        with mock.patch("codex_hooks.subprocess.run",
                        side_effect=[_run(stdout="codex-cli 1.2.3\n"), _run(stdout="Logged in\n")]):
            lines = self._all()
        self.assertFalse([l for l in lines if l[0] in ("WARN", "FAIL")], lines)
        self.assertTrue(any("codex-cli 1.2.3" in m for _, m in lines))
        self.assertTrue(any("Logged in" in m for _, m in lines))

    def test_missing_toml_is_flagged(self):
        self._install_all()
        (self.home / "agents" / "worker-medium.toml").unlink()
        status, msg = codex_hooks.workers_status(self.home / "agents")
        self.assertEqual(status, "WARN")
        self.assertIn("worker-medium.toml missing", msg)
        (self.home / "agents" / "worker-medium.toml").write_text("name = 'x'\n", encoding="utf-8")
        self.assertIn("worker-medium.toml differs", codex_hooks.workers_status(self.home / "agents")[1])

    def test_missing_skill_is_flagged(self):
        self._install_all()
        (self.claude / "skills" / "codex-worker" / "SKILL.md").unlink()
        self.assertEqual(onboarding.stale_claude_skills(self.claude), ["codex-worker"])

    def test_missing_git_guard_hook_is_flagged(self):
        self._install_all()
        path = self.home / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["hooks"]["PreToolUse"] = [e for e in data["hooks"]["PreToolUse"]
                                       if "git-guard.py" not in json.dumps(e)]
        path.write_text(json.dumps(data), encoding="utf-8")
        status, msg = codex_hooks.hooks_status(path)
        self.assertEqual(status, "WARN")
        self.assertIn("git-guard.py", msg)

    def test_codex_not_installed_is_one_info_line(self):
        with mock.patch("codex_hooks.subprocess.run", side_effect=AssertionError("must not run")):
            lines = codex_hooks.cli_statuses(None)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], "INFO")
        self.assertIn("skipped", lines[0][1])

    def test_login_failure_warns_and_falls_back_to_auth_file(self):
        with mock.patch("codex_hooks.subprocess.run", return_value=_run(1, "Not logged in\n")):
            self.assertEqual(codex_hooks.login_status("codex", self.home)[0], "WARN")
        with mock.patch("codex_hooks.subprocess.run", side_effect=OSError("gone")):
            self.assertEqual(codex_hooks.login_status("codex", self.home)[0], "WARN")
            (self.home / "auth.json").write_text("{}", encoding="utf-8")
            self.assertEqual(codex_hooks.login_status("codex", self.home)[0], "OK")

    def test_sandbox_windows_setting_and_effort_info(self):
        cfg = self.home / "config.toml"
        cfg.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
        self.assertEqual(codex_hooks.sandbox_status(cfg, windows=True)[0], "WARN")
        self.assertEqual(codex_hooks.sandbox_status(cfg, windows=False)[0], "INFO")
        cfg.write_text('model_reasoning_effort = "high"\n[windows]\nsandbox = "elevated"\n'
                       '[sandbox_workspace_write]\nwritable_roots = ["C:/Dev/vault"]\n', encoding="utf-8")
        status, msg = codex_hooks.sandbox_status(cfg, windows=True)
        self.assertEqual(status, "INFO")
        self.assertIn("C:/Dev/vault", msg)
        self.assertEqual(codex_hooks.effort_status(cfg)[0], "INFO")
        cfg.write_text('model_reasoning_effort = "medium"\n', encoding="utf-8")
        self.assertIsNone(codex_hooks.effort_status(cfg))


if __name__ == "__main__":
    unittest.main()
