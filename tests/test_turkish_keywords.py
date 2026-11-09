"""Tests for Turkish-aware keyword retrieval in tools/graph.py (no Ollama, no real vault)."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

GRAPH_PATH = Path(__file__).resolve().parent.parent / "tools" / "graph.py"
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)

    def note(self, nid: str, body: str, keywords: str = "") -> None:
        fm = f"---\nkeywords: [{keywords}]\n---\n" if keywords else ""
        write(self.data / f"{nid}.md", f"{fm}# {nid}\n\n{body}\n")

    def found(self, text: str) -> list[str]:
        g = graph.Graph(self.paths)
        return [r["id"] for r in graph.retrieve(g, text, [], graph.DEFAULT_THRESHOLD,
                                                graph.DEFAULT_DEPTH, include_core=False,
                                                semantic=False)]


class TestNormalization(unittest.TestCase):
    def test_turkish_case_and_ascii_forms_collapse(self):
        self.assertEqual(graph.tokenize("İSTANBUL istanbul"), ["istanbul", "istanbul"])
        self.assertEqual(graph.normalize_token("IŞIK"), "isik")
        self.assertEqual(graph.normalize_token("ışık"), "isik")
        self.assertEqual(graph.normalize_token("şifre"), "sifre")
        self.assertEqual(graph.normalize_token("sifre"), "sifre")

    def test_result_is_ascii_for_turkish_letters(self):
        self.assertEqual(graph.normalize_token("çğıöşüÇĞİÖŞÜ"), "cgiosucgiosu")

    def test_stopwords_use_normalized_form(self):
        for word in ("için", "nasıl", "şu", "NASIL"):
            self.assertIn(graph.normalize_token(word), graph.STOPWORDS)

    def test_stopwords_dropped_from_query(self):
        self.assertEqual(
            [t for t in graph.tokenize("şifre için nasıl") if len(t) >= 3 and t not in graph.STOPWORDS],
            ["sifre"],
        )


class TestEndToEnd(_Vault):
    def test_ascii_query_finds_turkish_keyword(self):
        self.note("kasa", "Gizli anahtarlar burada.", keywords="şifre")
        self.note("other", "Unrelated text.", keywords="deploy")
        self.assertEqual(self.found("sifre"), ["kasa"])

    def test_turkish_query_finds_ascii_keyword(self):
        self.note("kasa", "Gizli anahtarlar burada.", keywords="sifre")
        self.assertEqual(self.found("ŞİFRE"), ["kasa"])


if __name__ == "__main__":
    unittest.main()
