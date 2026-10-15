"""Tests for tools/model_config.py: model ranking and the vault.config.json +
machine.json config layering shared by `graph.py models`, doctor and
agent-guard.py. Standard library `unittest` only; no real files are touched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))
import model_config as mc  # noqa: E402


class TestRanking(unittest.TestCase):
    def test_ranking_order(self):
        self.assertLess(mc.rank("haiku"), mc.rank("sonnet"))
        self.assertLess(mc.rank("sonnet"), mc.rank("opus"))

    def test_unknown_model_ranks_none(self):
        self.assertIsNone(mc.rank("gpt-5"))
        self.assertIsNone(mc.rank(None))
        self.assertIsNone(mc.rank(123))

    def test_case_insensitive(self):
        self.assertEqual(mc.rank("Opus"), mc.rank("opus"))

    def test_variant_suffix_ignored_in_rank(self):
        self.assertEqual(mc.rank("opus[1m]"), mc.rank("opus"))
        self.assertEqual(mc.rank(" Opus[1M] "), mc.rank("opus"))
        self.assertIsNone(mc.rank("[1m]"))

    def test_configured_model_defaults_and_layered(self):
        self.assertEqual(mc.configured_model("worker-low"), "sonnet")
        eff = {"worker-low": {"model": "haiku", "effort": "low"}}
        self.assertEqual(mc.configured_model("worker-low", eff), "haiku")


class TestLayering(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.shared = self.tmp / "vault.config.json"
        self.machine = self.tmp / "machine.json"

    def test_no_files_returns_builtin_defaults(self):
        effective, sources = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective, mc.DEFAULT_MODELS)
        for role in mc.ROLES:
            self.assertEqual(sources[role], {"model": "default", "effort": "default"})

    def test_no_models_key_returns_builtin_defaults(self):
        self.shared.write_text(json.dumps({"schema": 1}), encoding="utf-8")
        effective, _ = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective, mc.DEFAULT_MODELS)

    def test_shared_overrides_default(self):
        self.shared.write_text(
            json.dumps({"models": {"orchestrator": {"model": "sonnet"}}}), encoding="utf-8")
        effective, sources = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective["orchestrator"]["model"], "sonnet")
        self.assertEqual(effective["orchestrator"]["effort"], "medium")  # untouched default
        self.assertEqual(sources["orchestrator"]["model"], "vault.config.json")
        self.assertEqual(sources["orchestrator"]["effort"], "default")

    def test_machine_overrides_shared_per_key(self):
        self.shared.write_text(
            json.dumps({"models": {"orchestrator": {"model": "sonnet", "effort": "high"}}}),
            encoding="utf-8")
        self.machine.write_text(
            json.dumps({"models": {"orchestrator": {"model": "opus"}}}), encoding="utf-8")
        effective, sources = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective["orchestrator"]["model"], "opus")  # machine wins
        self.assertEqual(effective["orchestrator"]["effort"], "high")  # shared value kept
        self.assertEqual(sources["orchestrator"]["model"], "machine.json")
        self.assertEqual(sources["orchestrator"]["effort"], "vault.config.json")

    def test_invalid_json_falls_back_to_defaults(self):
        self.shared.write_text("not json", encoding="utf-8")
        effective, _ = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective, mc.DEFAULT_MODELS)

    def test_unknown_role_is_ignored(self):
        self.shared.write_text(
            json.dumps({"models": {"nonsense-role": {"model": "opus"}}}), encoding="utf-8")
        effective, _ = mc.load_layered(self.shared, self.machine)
        self.assertEqual(effective, mc.DEFAULT_MODELS)


if __name__ == "__main__":
    unittest.main()
