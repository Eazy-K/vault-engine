"""Tests for search without embeddings and for Ollama problems in tools/graph.py.

Ollama is never contacted: urlopen is patched with canned answers.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import redirect_stderr
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

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


class TestKeywordFallback(_Vault):
    def test_common_body_words_do_not_load_notes(self):
        # A new vault: the only matches are everyday words in rule notes.
        self.note("rules", "Add a short line to the page when you change a rule.")
        self.note("inbox", "Add the task, then close it.")
        self.assertEqual(self.found("add login page"), [])

    def test_keyword_match_still_found(self):
        self.note("auth", "How sign-in works.", keywords="login, auth")
        self.note("rules", "Add a short line to the page when you change a rule.")
        self.assertEqual(self.found("add login page"), ["auth"])

    def test_note_matching_most_of_the_query_in_its_body_is_found(self):
        self.note("payments", "The checkout service retries a failed payment twice.")
        self.note("rules", "Fix a rule when it is wrong.")
        self.assertEqual(self.found("fix checkout payment service retries"), ["payments"])


def _http_404(url: str, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", {}, BytesIO(body))


class TestOllamaProblems(_Vault):
    def setUp(self):
        super().setUp()
        self.note("rules", "Some rule.")
        self.calls = []

    def _semantic(self, embed_answer, tags_answer=b'{"models": [{"name": "bge-m3:latest"}]}'):
        def urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            self.calls.append((url.rsplit("/", 1)[-1], timeout))
            answer = tags_answer if url.endswith("/api/tags") else embed_answer
            if isinstance(answer, Exception):
                raise answer
            return BytesIO(answer)

        err = StringIO()
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=urlopen), \
             redirect_stderr(err):
            result = graph.semantic_scores(graph.Graph(self.paths), "some query")
        return result, err.getvalue()

    def test_missing_model_prints_pull_command(self):
        result, err = self._semantic(_http_404(
            "http://x/api/embed", b'{"error":"model \\"bge-m3\\" not found, try pulling it first"}'))
        self.assertIsNone(result)
        self.assertIn(f"ollama pull {graph.EMBED_MODEL}", err)
        self.assertNotIn("HTTP Error 404", err)

    def test_model_missing_from_tags_skips_the_long_embed_call(self):
        # /api/tags answers fine but lists no bge-m3: fail from the probe alone,
        # instead of waiting on an /api/embed call that was never going to work.
        result, err = self._semantic(TimeoutError("should not be reached"),
                                      tags_answer=b'{"models": [{"name": "llama3:8b"}]}')
        self.assertIsNone(result)
        self.assertEqual([c[0] for c in self.calls], ["tags"])
        self.assertIn(f"ollama pull {graph.EMBED_MODEL}", err)

    def test_old_ollama_without_embed_route_is_told_apart(self):
        # bge-m3 is pulled (so the probe lets the call through), but this Ollama
        # predates the /api/embed route and answers 404 for the route itself.
        result, err = self._semantic(
            _http_404("http://x/api/embed", b"404 page not found"),
            tags_answer=b'{"models": [{"name": "bge-m3:latest"}]}')
        self.assertIsNone(result)
        self.assertIn("up to date", err)
        self.assertIn("ollama pull", err)

    def test_no_answer_fails_after_the_short_probe(self):
        hung = urllib.error.URLError(TimeoutError("timed out"))
        result, err = self._semantic(b"{}", tags_answer=hung)
        self.assertIsNone(result)
        # Only the probe ran, with its short timeout; the long embed call never started.
        self.assertEqual(self.calls, [("tags", graph.PROBE_TIMEOUT)])
        self.assertIn("no answer from Ollama", err)

    def test_http_error_on_probe_still_tries_embedding(self):
        self._semantic(_http_404("http://x/api/embed", b"model not found"),
                       tags_answer=_http_404("http://x/api/tags", b""))
        self.assertEqual([c[0] for c in self.calls], ["tags", "embed"])

    def test_embeddings_used_when_ollama_answers(self):
        result, err = self._semantic(b'{"embeddings": [[1.0, 0.0], [1.0, 0.0]]}')
        self.assertEqual(err, "")
        self.assertEqual(set(result), {"rules"})


class TestModelList(unittest.TestCase):
    def test_pulled_model_found_with_or_without_tag(self):
        with mock.patch.object(graph, "EMBED_MODEL", "bge-m3"):
            self.assertTrue(graph.has_embed_model(["llama3:latest", "bge-m3:latest"]))
            self.assertTrue(graph.has_embed_model(["bge-m3"]))
            self.assertFalse(graph.has_embed_model(["bge-m3:567m"]))
            self.assertFalse(graph.has_embed_model([]))
        with mock.patch.object(graph, "EMBED_MODEL", "bge-m3:567m"):
            self.assertTrue(graph.has_embed_model(["bge-m3:567m"]))
            self.assertFalse(graph.has_embed_model(["bge-m3:latest"]))

    def test_models_read_from_tags(self):
        body = b'{"models": [{"name": "bge-m3:latest"}, {"name": "llama3:8b"}]}'
        with mock.patch.object(graph.urllib.request, "urlopen", return_value=BytesIO(body)):
            self.assertEqual(graph.ollama_models(), ["bge-m3:latest", "llama3:8b"])

    def test_unexpected_tags_answer_is_a_value_error(self):
        with mock.patch.object(graph.urllib.request, "urlopen", return_value=BytesIO(b"[1, 2]")):
            with self.assertRaises(ValueError):
                graph.ollama_models()


if __name__ == "__main__":
    unittest.main()
