"""tools/budgets.py: config loading with fallbacks and validation."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import budgets  # noqa: E402


class TestBudgets(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.data, ignore_errors=True))

    def _config(self, value):
        text = value if isinstance(value, str) else json.dumps(value)
        (self.data / "vault.config.json").write_text(text, encoding="utf-8", newline="\n")

    def test_fallbacks_without_config(self):
        for data_dir in (None, self.data):
            b = budgets.load_budgets(data_dir)
            self.assertEqual(b["classes"], budgets.FALLBACK_CLASSES)
            self.assertEqual(b["roles"], budgets.FALLBACK_ROLES)
            self.assertEqual(set(b), {"classes", "roles"})
            self.assertEqual(b["classes"]["L"], {"max_turns": 150, "max_tokens": 600000})
            self.assertNotIn("L", b["roles"].values())

    def test_config_overrides(self):
        self._config({"budgets": {
            "classes": {"M": {"max_turns": 3, "max_tokens": 1000}},
            "roles": {"worker-medium": "M"}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(budgets.limits_for_role(b, "worker-medium"), ("M", 3, 1000))
        self.assertIsNone(budgets.limits_for_role(b, "worker-low"))  # S no longer exists

    def test_task_total_tokens_ignored(self):
        self._config({"budgets": {"task_total_tokens": 5000}})
        b = budgets.load_budgets(self.data)
        self.assertNotIn("task_total_tokens", b)
        self.assertEqual(b, budgets.load_budgets(None))

    def test_class_l_loads_from_config(self):
        self._config({"budgets": {
            "classes": {"L": {"max_turns": 150, "max_tokens": 600000}},
            "roles": {"worker-medium": "L"}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(budgets.limits_for_role(b, "worker-medium"), ("L", 150, 600000))

    def test_invalid_config_ignored(self):
        for bad in ("not json {", "[1]", {"budgets": "x"},
                    {"budgets": {"classes": {"M": {"max_turns": 0, "max_tokens": 5}}}},
                    {"budgets": {"classes": {"M": {"max_turns": True, "max_tokens": 5}}}},
                    {"budgets": {"classes": {"M": {"max_turns": "3", "max_tokens": 5}}}}):
            self._config(bad)
            b = budgets.load_budgets(self.data)
            self.assertEqual(b["classes"], budgets.FALLBACK_CLASSES, bad)

    def test_role_with_unknown_class_dropped(self):
        self._config({"budgets": {"roles": {"worker-low": "XL", "worker-medium": "S"}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(b["roles"], {"worker-medium": "S"})

    def test_limits_for_role(self):
        b = budgets.load_budgets(None)
        self.assertEqual(budgets.limits_for_role(b, "worker-low"), ("S", 20, 80000))
        self.assertEqual(budgets.limits_for_role(b, "worker-verify"), ("verify", 25, 100000))
        self.assertEqual(budgets.limits_for_role(b, "worker-codex"), ("S", 20, 80000))
        self.assertEqual(budgets.class_for_role(b, "worker-medium"), "M")
        self.assertIsNone(budgets.limits_for_role(b, "Explore"))
        self.assertIsNone(budgets.class_for_role(b, "Explore"))

    def test_data_dir_from_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_DATA", None)
            os.environ.pop("VAULT_HOME", None)
            self.assertIsNone(budgets.data_dir_from_env())
            os.environ["VAULT_HOME"] = str(self.data)
            self.assertEqual(budgets.data_dir_from_env(), self.data)
            os.environ["VAULT_DATA"] = str(self.data / "d")
            self.assertEqual(budgets.data_dir_from_env(), self.data / "d")


if __name__ == "__main__":
    unittest.main()
