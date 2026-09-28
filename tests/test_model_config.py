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

    def test_is_cheaper(self):
        self.assertTrue(mc.is_cheaper("haiku", "sonnet"))
        self.assertTrue(mc.is_cheaper("sonnet", "opus"))
        self.assertFalse(mc.is_cheaper("opus", "sonnet"))
        self.assertFalse(mc.is_cheaper("sonnet", "sonnet"))

    def test_is_cheaper_unknown_models_false(self):
        self.assertFalse(mc.is_cheaper("gpt-5", "sonnet"))
        self.assertFalse(mc.is_cheaper("sonnet", "gpt-5"))

    def test_cheaper_than(self):
        self.assertEqual(mc.cheaper_than("opus"), ["haiku", "sonnet"])
        self.assertEqual(mc.cheaper_than("sonnet"), ["haiku"])
        self.assertEqual(mc.cheaper_than("haiku"), [])

    def test_cheaper_than_unknown_model_empty(self):
        self.assertEqual(mc.cheaper_than("gpt-5"), [])

    def test_allowed_worker_models_defaults_to_todays_behaviour(self):
        self.assertEqual(set(mc.allowed_worker_models(None)), {"sonnet", "haiku"})
        self.assertEqual(set(mc.allowed_worker_models("opus")), {"sonnet", "haiku"})

    def test_allowed_worker_models_sonnet_orchestrator(self):
        self.assertEqual(mc.allowed_worker_models("sonnet"), ["haiku"])


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
