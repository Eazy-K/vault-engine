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
            self.assertEqual(b, {"roles": {"worker-low": 20, "worker-medium": 60,
                                           "worker-verify": 25, "worker-design": 60,
                                           "worker-design-verify": 25}})
            self.assertEqual(b["roles"], budgets.FALLBACK_ROLES)

    def test_config_overrides_per_role(self):
        self._config({"budgets": {"roles": {"worker-medium": 3, "worker-low": 0,
                                            "worker-verify": "x", "custom": 7}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(budgets.role_turn_cap(b, "worker-medium"), 3)
        self.assertEqual(budgets.role_turn_cap(b, "worker-low"), 20)  # invalid: fallback
        self.assertEqual(budgets.role_turn_cap(b, "worker-verify"), 25)
        self.assertEqual(budgets.role_turn_cap(b, "custom"), 7)
        self.assertIsNone(budgets.role_turn_cap(b, "Explore"))

    def test_design_role_caps_and_agent_max_turns(self):
        b = budgets.load_budgets(None)
        self.assertEqual(budgets.role_turn_cap(b, "worker-design"), 60)
        self.assertEqual(budgets.role_turn_cap(b, "worker-design-verify"), 25)
        self.assertEqual(budgets.agent_max_turns(60), 90)
        self.assertEqual(budgets.agent_max_turns(25), 38)
        self._config({"budgets": {"roles": {"worker-design": 40}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(budgets.role_turn_cap(b, "worker-design"), 40)
        self.assertEqual(budgets.role_turn_cap(b, "worker-design-verify"), 25)

    def test_task_total_tokens_ignored(self):
        self._config({"budgets": {"task_total_tokens": 5000}})
        b = budgets.load_budgets(self.data)
        self.assertNotIn("task_total_tokens", b)
        self.assertEqual(b, budgets.load_budgets(None))

    def test_old_shape_is_read(self):
        self._config({"budgets": {
            "classes": {"M": {"max_turns": 3, "max_tokens": 1000},
                        "verify": {"max_turns": 9, "max_tokens": 5}},
            "roles": {"worker-medium": "M", "worker-verify": "verify", "worker-low": "Q"}}})
        b = budgets.load_budgets(self.data)
        self.assertEqual(budgets.role_turn_cap(b, "worker-medium"), 3)
        self.assertEqual(budgets.role_turn_cap(b, "worker-verify"), 9)
        self.assertEqual(budgets.role_turn_cap(b, "worker-low"), 20)  # unknown class
        self.assertTrue(budgets.old_shape_in_config(self.data))

    def test_new_shape_is_not_old(self):
        self._config({"budgets": {"roles": {"worker-low": 5}, "context": {}}})
        self.assertFalse(budgets.old_shape_in_config(self.data))
        self.assertFalse(budgets.old_shape_in_config(None))

    def test_invalid_config_ignored(self):
        for bad in ("not json {", "[1]", {"budgets": "x"}, {"budgets": {"roles": [1]}},
                    {"budgets": {"roles": {"worker-low": 0}}},
                    {"budgets": {"roles": {"worker-low": True}}},
                    {"budgets": {"roles": {"worker-low": "3"}}}):
            self._config(bad)
            b = budgets.load_budgets(self.data)
            self.assertEqual(b["roles"], budgets.FALLBACK_ROLES, bad)

    def test_data_dir_from_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VAULT_DATA", None)
            os.environ.pop("VAULT_HOME", None)
            self.assertIsNone(budgets.data_dir_from_env())
            os.environ["VAULT_HOME"] = str(self.data)
            self.assertEqual(budgets.data_dir_from_env(), self.data)
            os.environ["VAULT_DATA"] = str(self.data / "d")
            self.assertEqual(budgets.data_dir_from_env(), self.data / "d")


def _rec(msg_id=None, usage=None):
    message = {"role": "assistant", "content": []}
    if msg_id is not None:
        message["id"] = msg_id
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "message": message}


class TestAgentMaxTurns(unittest.TestCase):
    def test_margin_rounds_up(self):
        self.assertEqual(budgets.MAX_TURNS_MARGIN, 1.5)
        got = {n: budgets.agent_max_turns(n) for n in (20, 60, 150, 25, 33, 1)}
        self.assertEqual(got, {20: 30, 60: 90, 150: 225, 25: 38, 33: 50, 1: 2})


class TestTranscriptUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _usage(self, records, raw_extra=""):
        path = self.tmp / "t.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n" + raw_extra,
                        encoding="utf-8", newline="\n")
        return budgets.transcript_usage(path)

    def test_shared_message_id_is_one_turn(self):
        self.assertEqual(self._usage([_rec("m1"), _rec("m1"), _rec("m1")]), (1, None))

    def test_records_without_id_count_individually(self):
        self.assertEqual(self._usage([_rec(), _rec(), _rec()])[0], 3)

    def test_mixed(self):
        records = [_rec("a"), _rec("a"), _rec("b"), _rec(), _rec(), {"type": "user"}]
        self.assertEqual(self._usage(records)[0], 4)

    def test_tokens_from_last_assistant_usage(self):
        records = [_rec("a", {"input_tokens": 1}),
                   _rec("b", {"input_tokens": 10, "cache_creation_input_tokens": 20,
                              "cache_read_input_tokens": 30, "output_tokens": 99})]
        self.assertEqual(self._usage(records), (2, 60))

    def test_bad_lines_and_missing_file(self):
        self.assertEqual(self._usage([_rec("a")], raw_extra="not json\n[1]\n"), (1, None))
        self.assertEqual(budgets.transcript_usage(self.tmp / "nope.jsonl"), (0, None))
        self.assertEqual(budgets.transcript_usage(None), (0, None))


if __name__ == "__main__":
    unittest.main()
