"""Tests for tools/models.py (`graph.py models`): reading/writing the shared
model-selection config and applying it to settings.json + worker-*.md.

Every test uses temp dirs for the data repo, settings.json and agents dir; the
real ~/.claude and vault.config.json are never read or written. Standard
library `unittest` only.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR"):
    import os
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
GRAPH_PATH = TOOLS_DIR / "graph.py"

_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS_DIR))
import models  # noqa: E402

WORKER_LOW_TEMPLATE = (TOOLS_DIR / "claude-agents" / "worker-low.md").read_text(encoding="utf-8")
WORKER_MEDIUM_TEMPLATE = (TOOLS_DIR / "claude-agents" / "worker-medium.md").read_text(encoding="utf-8")


def _ns(**kwargs) -> Namespace:
    base = dict(orchestrator=None, effort=None, worker_low=None, worker_low_effort=None,
                worker_medium=None, worker_medium_effort=None, this_computer=False,
                apply=False, data=None, settings=None, agents_dir=None)
    base.update(kwargs)
    return Namespace(**base)


class ModelsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "vault.config.json").write_text(json.dumps({"schema": 1}), encoding="utf-8")
        self.settings = self.tmp / "settings.json"
        self.agents = self.tmp / "agents"
        self.agents.mkdir()

    def _run(self, **kwargs) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            models.cmd_models(_ns(data=str(self.data), settings=str(self.settings),
                                   agents_dir=str(self.agents), **kwargs))
        return buf.getvalue()

    def _write_agent_files(self):
        (self.agents / "worker-low.md").write_text(WORKER_LOW_TEMPLATE, encoding="utf-8")
        (self.agents / "worker-medium.md").write_text(WORKER_MEDIUM_TEMPLATE, encoding="utf-8")


class TestShowDefaults(ModelsTestCase):
    def test_show_prints_builtin_defaults_when_unconfigured(self):
        out = self._run()
        self.assertIn("orchestrator: model=opus (default)", out)
        self.assertIn("worker-low: model=sonnet (default)", out)
        self.assertIn("worker-medium: model=sonnet (default)", out)

    def test_show_does_not_write_anything(self):
        self._run()
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("models", config)

    def test_show_reports_missing_settings_and_agents(self):
        out = self._run()
        self.assertIn("not found", out)


class TestSetConfig(ModelsTestCase):
    def test_set_orchestrator_writes_shared_config(self):
        self._run(orchestrator="sonnet", effort="high")
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["orchestrator"], {"model": "sonnet", "effort": "high"})

    def test_this_computer_writes_machine_json_not_shared(self):
        self._run(orchestrator="sonnet", this_computer=True)
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("models", config)
        machine = json.loads((self.data / ".graph" / "machine.json").read_text(encoding="utf-8"))
        self.assertEqual(machine["models"]["orchestrator"]["model"], "sonnet")

    def test_set_preserves_other_config_keys(self):
        self._run(orchestrator="sonnet")
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schema"], 1)

    def test_set_preserves_other_roles_and_fields(self):
        self._run(worker_low="haiku")
        self._run(orchestrator="sonnet")
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["worker-low"]["model"], "haiku")
        self.assertEqual(config["models"]["orchestrator"]["model"], "sonnet")

    def test_worker_downgraded_automatically_when_no_longer_cheaper(self):
        out = self._run(orchestrator="sonnet")
        self.assertIn("downgrading worker-low", out)
        self.assertIn("downgrading worker-medium", out)
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["worker-low"]["model"], "haiku")
        self.assertEqual(config["models"]["worker-medium"]["model"], "haiku")

    def test_explicit_worker_model_not_cheaper_refuses(self):
        with self.assertRaises(SystemExit):
            self._run(orchestrator="sonnet", worker_low="sonnet")
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertNotIn("models", config)

    def test_explicit_worker_model_cheaper_is_accepted(self):
        self._run(orchestrator="opus", worker_low="haiku")
        config = json.loads((self.data / "vault.config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["models"]["worker-low"]["model"], "haiku")


class TestApply(ModelsTestCase):
    def test_apply_writes_settings_and_agent_files(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", worker_medium="haiku", apply=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["model"], "sonnet")
        self.assertEqual(settings["effortLevel"], "medium")
        low = (self.agents / "worker-low.md").read_text(encoding="utf-8")
        self.assertIn("model: haiku", low)
        self.assertIn("effort: low", low)

    def test_apply_preserves_other_settings_keys(self):
        self.settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self._run(orchestrator="sonnet", apply=True)
        settings = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(settings["model"], "sonnet")

    def test_apply_backs_up_settings_before_changing(self):
        self.settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        self._run(orchestrator="sonnet", apply=True)
        backups = list(self.tmp.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["model"], "opus")

    def test_apply_preserves_agent_file_body_and_other_frontmatter(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", apply=True)
        low = (self.agents / "worker-low.md").read_text(encoding="utf-8")
        self.assertIn("name: worker-low", low)
        self.assertIn("You are a worker subagent.", low)

    def test_apply_backs_up_agent_file_before_changing(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", apply=True)
        backups = list(self.agents.glob("worker-low.md.bak-*"))
        self.assertEqual(len(backups), 1)

    def test_apply_is_idempotent(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", worker_medium="haiku", apply=True)
        settings_before = self.settings.read_text(encoding="utf-8")
        self._run(apply=True)
        settings_after = self.settings.read_text(encoding="utf-8")
        self.assertEqual(settings_before, settings_after)
        backups = list(self.tmp.glob("settings.json.bak-*"))
        self.assertEqual(backups, [])

    def test_apply_skips_missing_agent_files_without_error(self):
        # No worker-*.md written in the agents dir: --apply must not crash.
        out = self._run(orchestrator="sonnet", apply=True)
        self.assertIn("applied:", out)

    def test_show_reports_out_of_date_before_apply(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet")
        out = self._run()
        self.assertIn("OUT OF DATE", out)


class TestVariantSuffix(ModelsTestCase):
    def _settings(self, **kw):
        self.settings.write_text(json.dumps(kw), encoding="utf-8")

    def _read(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def _eff(self, model):
        return {"orchestrator": {"model": model, "effort": "medium"}}

    def test_suffixless_config_matches_suffixed_settings(self):
        self._settings(model="opus[1m]", effortLevel="medium")
        self.assertTrue(models.settings_matches(self._eff("opus"), self.settings))

    def test_suffixed_config_requires_exact_match(self):
        self._settings(model="opus", effortLevel="medium")
        self.assertFalse(models.settings_matches(self._eff("opus[1m]"), self.settings))
        self._settings(model="Opus[1M]", effortLevel="medium")
        self.assertTrue(models.settings_matches(self._eff("opus[1m]"), self.settings))

    def test_apply_keeps_suffix_when_only_effort_changes(self):
        self._settings(model="opus[1m]", effortLevel="low")
        models._apply_settings(self._eff("opus"), self.settings)
        self.assertEqual(self._read(), {"model": "opus[1m]", "effortLevel": "medium"})

    def test_apply_writes_suffixed_config_over_plain_settings(self):
        self._settings(model="opus", effortLevel="medium")
        self.assertTrue(models._apply_settings(self._eff("opus[1m]"), self.settings))
        self.assertEqual(self._read()["model"], "opus[1m]")

    def test_apply_replaces_different_base_model(self):
        self._settings(model="opus[1m]", effortLevel="medium")
        models._apply_settings(self._eff("sonnet"), self.settings)
        self.assertEqual(self._read()["model"], "sonnet")


class TestStatus(ModelsTestCase):
    def test_status_none_when_settings_missing(self):
        self.assertIsNone(models.status(self.data, self.settings, self.agents))

    def test_status_ok_after_apply(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", worker_medium="haiku", apply=True)
        level, msg = models.status(self.data, self.settings, self.agents)
        self.assertEqual(level, "OK")

    def test_status_warn_when_stale(self):
        self._write_agent_files()
        self._run(orchestrator="sonnet", worker_low="haiku", worker_medium="haiku", apply=True)
        self._run(orchestrator="opus", worker_low="sonnet", worker_medium="sonnet")
        level, msg = models.status(self.data, self.settings, self.agents)
        self.assertEqual(level, "WARN")
        self.assertIn("models --apply", msg)


class TestFrontmatterHelpers(unittest.TestCase):
    def test_worker_frontmatter_reads_model_and_effort(self):
        found = models.worker_frontmatter(WORKER_LOW_TEMPLATE)
        self.assertEqual(found["model"], "sonnet")
        self.assertEqual(found["effort"], "low")

    def test_worker_frontmatter_no_frontmatter_block(self):
        self.assertEqual(models.worker_frontmatter("no frontmatter here"), {})


if __name__ == "__main__":
    unittest.main()
