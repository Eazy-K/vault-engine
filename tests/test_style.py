"""Static checks over tools/*.py that keep cross-platform behaviour honest."""
from __future__ import annotations

import ast
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


class TestTextWrites(unittest.TestCase):
    def test_every_text_write_sets_newline(self):
        for path in sorted(TOOLS.glob("*.py")):
            with self.subTest(file=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertEqual(_text_writes_without_newline(tree), [],
                                 f"{path.name}: pass newline=\"\n\" on these lines")


if __name__ == "__main__":
    unittest.main()
