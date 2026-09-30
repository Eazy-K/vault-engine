"""user-config tests. Every target lives in a temporary CODEX_HOME / CLAUDE_CONFIG_DIR."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

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
import user_config as uc  # noqa: E402

BLOCK_SRC = "Line one {VAULT_DATA}\nLine two"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.engine = root / "engine"
        self.data = root / "ws" / "vault"
        self.codex = root / "codex"
        self.claude = root / "claude"
        for d in (self.engine / "defaults", self.data, self.codex, self.claude):
            d.mkdir(parents=True)
        self.paths = graph.Paths(self.engine, self.data)
        env = {"CODEX_HOME": str(self.codex), "CLAUDE_CONFIG_DIR": str(self.claude)}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(self._restore, old)

    @staticmethod
    def _restore(old):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def put(self, base: Path, rel: str, text: str) -> Path:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def run_cmd(self, install=False):
        out = io.StringIO()
        code = 0
        with redirect_stdout(out), mock.patch.object(graph, "ENGINE", self.engine):
            try:
                uc.cmd_user_config(Namespace(data=str(self.data), install=install, check=not install))
            except SystemExit as exc:
                code = exc.code or 0
        return code, out.getvalue()

    def backups(self, directory: Path):
        return sorted(p.name for p in directory.rglob("*.bak-*"))

    def items(self):
        return uc.evaluate(self.paths, self.codex, self.claude)


class TestSources(Base):
    def test_vault_file_overrides_engine_default(self):
        rel = uc.INSTRUCTIONS_SRC
        self.put(self.engine / "defaults", rel, "engine")
        self.assertEqual(uc.resolve_source(self.paths, rel), self.engine / "defaults" / rel)
        self.put(self.data, rel, "vault")
        self.assertEqual(uc.resolve_source(self.paths, rel), self.data / rel)

    def test_missing_source_is_skipped_silently(self):
        self.assertEqual(self.items(), [])
        code, _out = self.run_cmd()
        self.assertEqual(code, 0)

    def test_placeholders_expand_to_forward_slash_paths(self):
        values = uc.placeholders(self.paths)
        self.assertEqual(values["{WORKSPACE}"], self.data.parent.resolve().as_posix())
        text = uc.expand("{VAULT_DATA}|{VAULT_ENGINE}|{WORKSPACE}", values)
        self.assertNotIn("\\", text)
        self.assertNotIn("{", text)

    def test_blank_source_counts_as_absent(self):
        self.put(self.data, uc.RULES_SRC, "\n  \n")
        self.assertIsNone(uc.plan_rules(self.paths, self.codex))

    def test_engine_default_template_is_shipped(self):
        real = graph.Paths(REPO_ROOT, self.data)
        self.assertEqual(uc.plan_instructions(real, self.codex).status, "drift")

    def test_config_templates_are_not_notes(self):
        self.put(self.data, "config/codex/developer-instructions.md", "# x")
        self.put(self.data, "notes/a.md", "# A")
        self.assertEqual(sorted(graph.load_notes(self.data)), ["notes/a"])


class TestInstructions(Base):
    def setUp(self):
        super().setUp()
        self.put(self.data, uc.INSTRUCTIONS_SRC, BLOCK_SRC)
        self.cfg = self.codex / "config.toml"

    def install(self):
        return uc.install(self.items())

    def parsed(self):
        return tomllib.loads(self.cfg.read_text(encoding="utf-8"))

    def value(self):
        return self.parsed()[uc.KEY]

    def test_creates_file_and_expands_placeholder(self):
        self.install()
        value = self.value()
        self.assertIn(uc.BEGIN, value)
        self.assertIn(self.data.resolve().as_posix(), value)
        self.assertEqual(self.backups(self.codex), [])

    def test_key_missing_inserted_before_first_table(self):
        self.put(self.codex, "config.toml", 'model = "m"\n\n[projects.x]\ntrust_level = "trusted"\n')
        self.install()
        data = self.parsed()
        self.assertEqual(data["model"], "m")
        self.assertEqual(data["projects"]["x"]["trust_level"], "trusted")
        self.assertIn(uc.BEGIN, data[uc.KEY])
        self.assertEqual(len(self.backups(self.codex)), 1)

    def test_key_missing_no_tables(self):
        self.put(self.codex, "config.toml", 'model = "m"')
        self.install()
        data = self.parsed()
        self.assertEqual(data["model"], "m")
        self.assertIn("Line two", data[uc.KEY])

    def test_replace_block_preserves_outside_text(self):
        old = f"mine before\n{uc.BEGIN}\nold text\n{uc.END}\nmine after\n"
        self.put(self.codex, "config.toml", f"{uc.KEY} = '''\n{old}'''\n[t]\na = 1\n")
        self.install()
        value = self.value()
        self.assertTrue(value.startswith("mine before\n"))
        self.assertTrue(value.endswith("mine after\n"))
        self.assertNotIn("old text", value)
        self.assertIn("Line two", value)
        self.assertEqual(value.count(uc.BEGIN), 1)

    def test_basic_string_is_converted_and_kept(self):
        self.put(self.codex, "config.toml", f'{uc.KEY} = "keep \\"me\\" \\\\ ok"  # note\n[t]\na = 1\n')
        self.install()
        value = self.value()
        self.assertTrue(value.startswith('keep "me" \\ ok'))
        self.assertIn("Line one", value)
        self.assertEqual(self.parsed()["t"]["a"], 1)

    def test_multiline_basic_string(self):
        self.put(self.codex, "config.toml", f'{uc.KEY} = """\nuser text\n"""\n')
        self.install()
        self.assertTrue(self.value().startswith("user text\n"))

    def test_triple_quote_in_source_uses_escaped_string(self):
        self.put(self.data, uc.INSTRUCTIONS_SRC, "say '''hi'''")
        self.install()
        self.assertIn("say '''hi'''", self.value())
        self.assertEqual(self.install(), [])

    def test_other_multiline_strings_with_brackets_are_not_tables(self):
        self.put(self.codex, "config.toml", "x = '''\n[not a table]\n'''\n[t]\na = 1\n")
        self.install()
        data = self.parsed()
        self.assertEqual(data["x"], "[not a table]\n")
        self.assertIn(uc.KEY, data)

    def test_idempotent_no_second_backup(self):
        self.put(self.codex, "config.toml", 'model = "m"\n')
        self.assertEqual(len(self.install()), 1)
        snapshot = self.cfg.read_bytes()
        self.assertEqual(self.install(), [])
        self.assertEqual(self.cfg.read_bytes(), snapshot)
        self.assertEqual(len(self.backups(self.codex)), 1)

    def test_crlf_is_preserved(self):
        self.put(self.codex, "config.toml", 'model = "m"\r\n[t]\r\na = 1\r\n')
        self.install()
        raw = self.cfg.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(tomllib.loads(raw.decode("utf-8"))["t"]["a"], 1)
        self.assertEqual(self.install(), [])

    def test_invalid_toml_is_an_error_not_overwritten(self):
        self.put(self.codex, "config.toml", "this is = = bad")
        items = self.items()
        self.assertEqual(items[0].status, "error")
        self.assertEqual(uc.install(items), [])
        self.assertEqual(self.cfg.read_text(), "this is = = bad")

    def test_unbalanced_markers_are_an_error(self):
        self.put(self.codex, "config.toml", f"{uc.KEY} = '''\n{uc.BEGIN}\nx\n'''\n")
        self.assertEqual(self.items()[0].status, "error")


class TestRules(Base):
    def setUp(self):
        super().setUp()
        self.put(self.data, uc.RULES_SRC, '# rules\nprefix_rule(pattern=["git", "status"], decision="allow")')
        self.rules = self.codex / "rules" / "default.rules"

    def test_creates_dir_and_file_then_idempotent(self):
        uc.install(self.items())
        text = self.rules.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(uc.BEGIN))
        self.assertIn("git", text)
        self.assertEqual(uc.install(self.items()), [])
        self.assertEqual(self.backups(self.codex), [])

    def test_lines_outside_block_untouched(self):
        self.put(self.codex, "rules/default.rules", f"mine1\n\n{uc.BEGIN}\nstale\n{uc.END}\nmine2\n")
        uc.install(self.items())
        text = self.rules.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("mine1\n\n" + uc.BEGIN))
        self.assertTrue(text.endswith(uc.END + "\nmine2\n"))
        self.assertNotIn("stale", text)
        self.assertEqual(len(self.backups(self.codex)), 1)

    def test_appended_after_existing_content(self):
        self.put(self.codex, "rules/default.rules", "existing\n")
        uc.install(self.items())
        text = self.rules.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("existing\n\n" + uc.BEGIN))


class TestSettings(Base):
    def setUp(self):
        super().setUp()
        fragment = {"permissions": {"allow": ["Bash(git push:*)", "Bash(gh pr merge:*)"],
                                    "deny": ["Bash(git push --force:*)"]},
                    "statusLine": {"command": "{VAULT_ENGINE}/x"}, "flag": True}
        self.put(self.data, uc.SETTINGS_SRC, json.dumps(fragment))
        self.settings = self.claude / "settings.json"

    def test_deep_merge_semantics(self):
        merged = uc.deep_merge({"a": {"l": [1, 2], "s": 1, "keep": 0}, "z": [9]},
                               {"a": {"l": [2, 3], "s": 5}, "n": 1})
        self.assertEqual(merged, {"a": {"l": [1, 2, 3], "s": 5, "keep": 0}, "z": [9], "n": 1})

    def test_merge_preserves_existing_and_order(self):
        self.put(self.claude, "settings.json", json.dumps({
            "theme": "dark", "permissions": {"allow": ["Bash(ls:*)", "Bash(git push:*)"]}}))
        uc.install(self.items())
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["permissions"]["allow"],
                         ["Bash(ls:*)", "Bash(git push:*)", "Bash(gh pr merge:*)"])
        self.assertEqual(data["permissions"]["deny"], ["Bash(git push --force:*)"])
        self.assertEqual(data["statusLine"]["command"], self.engine.resolve().as_posix() + "/x")
        self.assertEqual(len(self.backups(self.claude)), 1)

    def test_idempotent(self):
        uc.install(self.items())
        self.assertEqual(uc.install(self.items()), [])
        self.assertEqual(self.backups(self.claude), [])

    def test_invalid_existing_json_is_an_error(self):
        self.put(self.claude, "settings.json", "{oops")
        self.assertEqual(uc.plan_settings(self.paths, self.claude).status, "error")


class TestCommand(Base):
    def test_check_exit_codes_and_install(self):
        self.put(self.data, uc.SETTINGS_SRC, '{"a": 1}')
        code, out = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertIn("WARN", out)
        self.assertIn("user-config --install", out)
        code, out = self.run_cmd(install=True)
        self.assertEqual(code, 0)
        self.assertIn("updated", out)
        code, _out = self.run_cmd()
        self.assertEqual(code, 0)
        _code, out = self.run_cmd(install=True)
        self.assertNotIn("updated", out)

    def test_install_error_exit_code(self):
        self.put(self.data, uc.SETTINGS_SRC, '{"a": 1}')
        self.put(self.claude, "settings.json", "{bad")
        code, out = self.run_cmd(install=True)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)

    def test_statuses_and_drift_only_for_existing_homes(self):
        self.put(self.data, uc.SETTINGS_SRC, '{"a": 1}')
        self.assertTrue(uc.has_drift(self.paths))
        self.assertEqual(uc.statuses(self.paths)[0][0], "WARN")
        uc.install(self.items())
        self.assertFalse(uc.has_drift(self.paths))
        self.assertEqual(uc.statuses(self.paths)[0][0], "OK")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.claude / "missing")
        self.assertEqual(uc.statuses(self.paths), [])


if __name__ == "__main__":
    unittest.main()
