"""Embedding isolation after an HTTP 400 from Ollama (tools/graph.py refresh_embeddings).

Ollama is never contacted: urlopen is patched with a fake /api/embed that refuses
chosen inputs. The fake records every request body so tests can check what was sent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME", "CLAUDE_CONFIG_DIR"):
    os.environ.pop(_var, None)


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("graph", REPO_ROOT / "tools" / "graph.py")
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)
sys.path.insert(0, str(REPO_ROOT / "tools"))
import onboarding  # noqa: E402

TAGS = b'{"models": [{"name": "bge-m3:latest"}]}'


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FakeOllama:
    """Fake /api/embed. `refuse(text)` is True for an input that makes the whole request
    answer HTTP 400. Vectors are [len(text), 1.0] so tests can tell inputs apart."""

    def __init__(self, refuse=lambda text: False, status: int = 400):
        self.refuse, self.status = refuse, status
        self.requests: list[list[str]] = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/tags"):
            return BytesIO(TAGS)
        texts = json.loads(req.data.decode("utf-8"))["input"]
        self.requests.append(texts)
        if self.status != 400:
            raise urllib.error.HTTPError(url, self.status, "x", {}, BytesIO(b"{}"))
        if any(self.refuse(t) for t in texts):
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, BytesIO(b"{}"))
        return BytesIO(json.dumps({"embeddings": [[float(len(t)), 1.0] for t in texts]}).encode())

    @property
    def sent(self) -> list[str]:
        return [t for batch in self.requests for t in batch]


class _Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)
        graph.RETRIEVAL.update(mode="keyword", reason="not run")

    def note(self, nid: str, body: str) -> None:
        write(self.data / f"{nid}.md", f"# {nid}\n\n{body}\n")

    def refresh(self, fake, query="some query"):
        err = StringIO()
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=fake), \
             redirect_stderr(err):
            cache, qvec = graph.refresh_embeddings(graph.Graph(self.paths), query)
        return cache, qvec, err.getvalue()

    def usage_events(self) -> list[dict]:
        path = self.paths.usage_log
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestBatchBisect(_Vault):
    def test_query_embedded_alone_and_good_chunks_cached(self):
        self.note("good", "- fine one\n- fine two")
        self.note("bad", "- BAD line")
        fake = FakeOllama(refuse=lambda t: "BAD" in t)
        cache, qvec, err = self.refresh(fake)
        self.assertIsNotNone(qvec)
        self.assertIn(["some query"], fake.requests)  # the query went alone
        self.assertEqual(len(cache["notes"]["good"]["vectors"]), 2)
        self.assertNotIn("skipped", cache["notes"]["good"])
        # the batch of every chunk was tried once, then split in halves
        self.assertGreater(len(fake.requests[0]), len(fake.requests[2]))
        self.assertTrue(any("BAD" in t for t in fake.requests[0]))

    def test_mode_stays_semantic(self):
        self.note("good", "- fine one")
        self.note("bad", "- BAD line")
        fake = FakeOllama(refuse=lambda t: "BAD" in t)
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=fake), \
             redirect_stderr(StringIO()):
            scores = graph.semantic_scores(graph.Graph(self.paths), "some query")
        self.assertIsNotNone(scores)
        self.assertEqual(set(scores), {"good", "bad"})
        graph.RETRIEVAL.update(mode="semantic", reason="")  # what seed() does next
        self.assertEqual(graph.RETRIEVAL["mode"], "semantic")

    def test_bad_query_alone_falls_back_to_keyword(self):
        self.note("good", "- fine one")
        fake = FakeOllama(refuse=lambda t: t == "some query")
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=fake), \
             redirect_stderr(StringIO()):
            self.assertIsNone(graph.semantic_scores(graph.Graph(self.paths), "some query"))
        self.assertEqual(graph.RETRIEVAL["reason"], "ollama error, HTTP 400")

    def test_healthy_batch_is_one_request(self):
        self.note("a", "- one\n- two")
        fake = FakeOllama()
        cache, qvec, _ = self.refresh(fake)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(len(fake.requests[0]), 3)  # 2 chunks + the query


class TestLosslessSplit(_Vault):
    def test_refused_chunk_of_any_size_is_split_not_truncated(self):
        words = " ".join(f"word{i:03d}" for i in range(60))  # ~480 chars
        self.note("long", f"- {words}")
        # Ollama refuses anything over 120 characters
        fake = FakeOllama(refuse=lambda t: len(t) > 120)
        cache, qvec, err = self.refresh(fake)
        self.assertNotIn("skipped", cache["notes"]["long"])
        self.assertGreater(len(cache["notes"]["long"]["vectors"]), 2)
        accepted = [t for t in fake.sent if len(t) <= 120]
        for i in range(60):
            self.assertTrue(any(f"word{i:03d}" in t for t in accepted), i)
        self.assertEqual(" ".join(w for t in accepted if t.startswith("long")
                                  for w in t.split(": ", 1)[1].split()),
                         words)

    def test_medium_chunk_300_to_600_is_split_too(self):
        text = " ".join(f"w{i:03d}" for i in range(70))  # ~350 chars
        self.note("mid", f"- {text}")
        fake = FakeOllama(refuse=lambda t: len(t) > 200)
        cache, _, _ = self.refresh(fake)
        self.assertNotIn("skipped", cache["notes"]["mid"])
        joined = " ".join(t.split(": ", 1)[1] for t in fake.sent
                          if len(t) <= 200 and t != "some query")
        self.assertEqual(joined.split(), text.split())

    def test_continuation_carries_context_prefix(self):
        self.note("long", "- " + " ".join(f"word{i:03d}" for i in range(60)))
        fake = FakeOllama(refuse=lambda t: len(t) > 200)
        self.refresh(fake)
        pieces = [t for t in fake.sent if t.startswith("long") and len(t) <= 200]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(t.startswith("long: ") for t in pieces))

    def test_split_chunk_floor_and_cut(self):
        self.assertIsNone(graph._split_chunk("t: " + "x" * graph.EMBED_SPLIT_FLOOR))
        a, b = graph._split_chunk("t: aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj kkkk llll")
        self.assertTrue(a.startswith("t: ") and b.startswith("t: "))
        self.assertEqual((a + b).replace("t: ", ""),
                         "aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj kkkk llll")
        # no whitespace at all: cut in the middle
        a, b = graph._split_chunk("t: " + "y" * 200)
        self.assertEqual(len(a) + len(b), 3 + 3 + 200)


class TestFloorAndSkip(_Vault):
    def test_sanitised_retry_succeeds(self):
        self.note("ctl", "- fine\x01 text")
        fake = FakeOllama(refuse=lambda t: "\x01" in t)
        cache, _, err = self.refresh(fake)
        self.assertNotIn("skipped", cache["notes"]["ctl"])
        self.assertEqual(len(cache["notes"]["ctl"]["vectors"]), 1)
        self.assertIn("ctl: fine text", fake.sent)  # the cleaned text, tried once
        self.assertEqual(fake.sent.count("ctl: fine text"), 1)
        self.assertEqual(self.usage_events(), [])

    def test_still_refused_chunk_is_skipped_and_reported_without_text(self):
        self.note("one", "- SECRETWORD refused always")
        self.note("two", "- fine")
        fake = FakeOllama(refuse=lambda t: "SECRETWORD" in t)
        cache, qvec, err = self.refresh(fake)
        self.assertIsNotNone(qvec)
        self.assertEqual(cache["notes"]["one"]["skipped"], [0])
        self.assertEqual(cache["notes"]["one"]["vectors"], [])
        self.assertNotIn("skipped", cache["notes"]["two"])
        self.assertEqual(len(cache["notes"]["two"]["vectors"]), 1)
        self.assertIn("one", err)
        self.assertNotIn("SECRETWORD", err)
        self.assertNotIn("refused", err)
        events = [e for e in self.usage_events() if e["event"] == "embed_skipped"]
        self.assertEqual([(e["note"], e["chunk"]) for e in events], [("one", 0)])
        self.assertNotIn("SECRETWORD", json.dumps(events))
        self.assertIn("HTTP 400", events[0]["reason"])
        # the cache is on disk and the skipped chunk is not retried next time
        fake2 = FakeOllama(refuse=lambda t: "SECRETWORD" in t)
        self.refresh(fake2)
        self.assertEqual(fake2.sent, ["some query"])

    def test_note_without_vectors_still_scores(self):
        self.note("one", "- SECRETWORD only")
        self.note("two", "- some query itself")
        fake = FakeOllama(refuse=lambda t: "SECRETWORD" in t)
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=fake), \
             redirect_stderr(StringIO()):
            scores = graph.semantic_scores(graph.Graph(self.paths), "some query")
        self.assertEqual(set(scores), {"one", "two"})  # no KeyError/ValueError on empty vectors
        self.assertLess(scores["one"], scores["two"])

    def test_old_cache_without_skipped_field_reads(self):
        self.note("a", "- fine")
        cache, _, _ = self.refresh(FakeOllama())
        self.assertNotIn("skipped", cache["notes"]["a"])
        fake = FakeOllama()
        cache2, _, _ = self.refresh(fake)
        self.assertEqual(fake.sent, ["some query"])  # unchanged note is not re-embedded

    def test_sanitize_drops_only_control_and_invalid(self):
        self.assertEqual(graph._sanitize_embed_text("a\x00b\x7fc\ud800d￾e\tf\ng çş"),
                         "abcde\tf\ng çş")


class TestOtherErrors(_Vault):
    def test_404_falls_back_without_retry(self):
        self.note("a", "- fine")
        fake = FakeOllama(status=404)
        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=fake), \
             redirect_stderr(StringIO()):
            self.assertIsNone(graph.semantic_scores(graph.Graph(self.paths), "some query"))
        self.assertEqual(len(fake.requests), 1)

    def test_connection_error_falls_back_without_retry(self):
        self.note("a", "- fine")
        calls = []

        def urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith("/api/tags"):
                return BytesIO(TAGS)
            calls.append(url)
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=urlopen), \
             redirect_stderr(StringIO()):
            self.assertIsNone(graph.semantic_scores(graph.Graph(self.paths), "some query"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(graph.RETRIEVAL["reason"], "ollama unreachable")

    def test_404_during_isolation_stops_the_bisect(self):
        self.note("a", "- BAD one")
        self.note("b", "- fine")
        state = {"n": 0}

        def urlopen(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith("/api/tags"):
                return BytesIO(TAGS)
            state["n"] += 1
            code = 400 if state["n"] == 1 else 404
            raise urllib.error.HTTPError(url, code, "x", {}, BytesIO(b"{}"))

        with mock.patch.object(graph.urllib.request, "urlopen", side_effect=urlopen), \
             redirect_stderr(StringIO()):
            self.assertIsNone(graph.semantic_scores(graph.Graph(self.paths), "some query"))
        self.assertEqual(state["n"], 2)  # the 400, then the query alone: 404, no more


class TestDoctorWarn(_Vault):
    def test_doctor_warns_for_note_with_skipped_chunks(self):
        self.note("one", "- SECRETWORD refused always")
        self.refresh(FakeOllama(refuse=lambda t: "SECRETWORD" in t))
        home = self.tmp / "home"
        home.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "VAULT_ENGINE": str(REPO_ROOT)}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("pathlib.Path.home", return_value=home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("no ollama")), \
             redirect_stdout(StringIO()) as buf, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace(data=str(self.data)))
        lines = [ln for ln in buf.getvalue().splitlines() if "could not be embedded" in ln]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("WARN one: 1 chunk(s) could not be embedded"))
        self.assertNotIn("SECRETWORD", lines[0])


if __name__ == "__main__":
    unittest.main()
