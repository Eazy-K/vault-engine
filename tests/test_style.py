"""Static checks over tools/*.py that keep cross-platform behaviour honest."""
from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _text_writes_without_newline(tree: ast.AST) -> list[int]:
    """Line numbers of write_text(...) / open("a"|"w", ...) calls without newline=.
    Without it Windows writes CRLF, which git then flags on every commit."""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        keywords = {k.arg for k in node.keywords}
        name = node.func.attr
        writes = name == "write_text" or (
            name == "open" and node.args and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value)[:1] in ("a", "w"))
        if writes and "newline" not in keywords:
            bad.append(node.lineno)
    return bad


def _schemas_missing_upgrade_note(changelog: str, schema_version: int) -> list[int]:
    """Schemas above 1 whose raise no CHANGELOG "Upgrade notes" section covers.
    Engines 0.1.0/0.2.0 have no schema check and would keep writing to a
    migrated vault, so the release that raises the schema must name it
    (`schema N`) and ask to bring every computer to 0.3.0 or later first."""
    notes = re.findall(r"^### Upgrade notes\n(.*?)(?=^##|\Z)", changelog, re.M | re.S)
    return [n for n in range(2, schema_version + 1)
            if not any(f"schema {n}" in body and "0.3.0" in body for body in notes)]


class TestTextWrites(unittest.TestCase):
    def test_every_text_write_sets_newline(self):
        for path in sorted(TOOLS.glob("*.py")):
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertEqual(_text_writes_without_newline(tree), [],
                                 f"{path.name}: pass newline=\"\n\" on these lines")


class TestVersion(unittest.TestCase):
    def test_changelog_top_entry_matches_version(self):
        # A release bumps both; forgetting one ships a misleading --version.
        source = (TOOLS / "graph.py").read_text(encoding="utf-8")
        version = re.search(r'^__version__ = "([^"]+)"', source, re.M).group(1)
        changelog = (TOOLS.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        top = re.search(r"^## \[([^\]]+)\]", changelog, re.M).group(1)
        self.assertEqual(top, version)

    def test_schema_raise_has_upgrade_note(self):
        source = (TOOLS / "schema.py").read_text(encoding="utf-8")
        schema_version = int(re.search(r"^SCHEMA_VERSION = (\d+)", source, re.M).group(1))
        changelog = (TOOLS.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(_schemas_missing_upgrade_note(changelog, schema_version), [],
                         "a release raising SCHEMA_VERSION needs an Upgrade note naming "
                         "`schema N` that asks for 0.3.0 or later on every computer first")

    def test_schema_upgrade_note_check(self):
        entry = ("## [0.9.0] - 2026-01-01\n\n### Added\n- schema 2 mentioned outside notes, 0.3.0\n\n"
                 "### Upgrade notes\n- {}\n\n## [0.8.0] - 2025-12-01\n")
        good = entry.format("Raises the vault to schema 2: first update every computer to 0.3.0 or later.")
        self.assertEqual(_schemas_missing_upgrade_note(good, 2), [])
        self.assertEqual(_schemas_missing_upgrade_note(good, 3), [3])
        self.assertEqual(_schemas_missing_upgrade_note(entry.format("Raises the vault to schema 2."), 2), [2])
        self.assertEqual(_schemas_missing_upgrade_note("# Changelog\n", 1), [])

    def test_tag_at_head_matches_version(self):
        # On a stable install HEAD is a release tag; it must match --version.
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            cwd=TOOLS.parent, capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("HEAD is not exactly at a tag (or git is unavailable)")
        tag = result.stdout.strip()
        source = (TOOLS / "graph.py").read_text(encoding="utf-8")
        version = re.search(r'^__version__ = "([^"]+)"', source, re.M).group(1)
        self.assertEqual(tag, "v" + version)


if __name__ == "__main__":
    unittest.main()
