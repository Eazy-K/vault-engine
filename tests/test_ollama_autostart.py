"""Tests for the Ollama autostart in tools/graph.py (task 0063).

No process is ever started and Ollama is never contacted: Popen, shutil.which, time
and urlopen are all patched.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

for _var in ("VAULT_DATA", "VAULT_HOME"):
    os.environ.pop(_var, None)
# Other test modules load their own copy of graph.py. The variable is read per call, so
# this keeps every test of a full discover run (all modules are imported before any
# test runs) from starting a real `ollama serve` when it takes the "nothing answers"
# path. The tests below turn it back on explicitly, per test.
os.environ.setdefault("VAULT_OLLAMA_AUTOSTART", "0")


def _no_registry(*_args):
    raise OSError("tests never read the real registry")


sys.modules["winreg"] = types.SimpleNamespace(HKEY_CURRENT_USER=None, OpenKey=_no_registry)

TOOLS = Path(__file__).resolve().parent.parent / "tools"
GRAPH_PATH = TOOLS / "graph.py"
_spec = importlib.util.spec_from_file_location("graph", GRAPH_PATH)
graph = importlib.util.module_from_spec(_spec)
sys.modules["graph"] = graph
_spec.loader.exec_module(graph)

sys.path.insert(0, str(TOOLS))
import onboarding  # noqa: E402

TAGS = b'{"models": [{"name": "bge-m3:latest"}]}'
EMBED = b'{"embeddings": [[1.0, 0.0], [1.0, 0.0]]}'
DOWN = urllib.error.URLError(ConnectionRefusedError("refused"))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Clock:
    """A fake time: sleep advances monotonic, nothing really waits."""
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


class _Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = self.tmp / "engine"
        self.data = self.tmp / "data"
        self.engine.mkdir()
        self.data.mkdir()
        self.paths = graph.Paths(self.engine, self.data)
        write(self.data / "rules.md", "---\nkeywords: []\n---\n# rules\n\nSome rule.\n")
        self.calls = []
        self.popen = mock.Mock()
        self.clock = _Clock()

    def _semantic(self, tags_answers, embed_answer=EMBED, which="/bin/ollama",
                  autostart="1", url=None, os_name=None):
        """tags_answers: one answer per /api/tags call; the last one repeats."""
        answers = list(tags_answers)

        def urlopen(req, timeout=None):
            url_ = req if isinstance(req, str) else req.full_url
            self.calls.append(url_.rsplit("/", 1)[-1])
            if url_.endswith("/api/tags"):
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
            else:
                answer = embed_answer
            if isinstance(answer, Exception):
                raise answer
            return BytesIO(answer)

        patches = [
            mock.patch.object(graph.urllib.request, "urlopen", side_effect=urlopen),
            mock.patch.object(graph.subprocess, "Popen", self.popen),
            mock.patch.object(graph.shutil, "which", return_value=which),
            mock.patch.object(graph.time, "monotonic", self.clock.monotonic),
            mock.patch.object(graph.time, "sleep", self.clock.sleep),
            mock.patch.dict(os.environ, {"VAULT_OLLAMA_AUTOSTART": autostart}),
        ]
        if url:
            patches.append(mock.patch.object(graph, "OLLAMA_URL", url))
        if os_name:
            patches.append(mock.patch.object(sys, "platform", os_name))
        err = StringIO()
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(redirect_stderr(err))
            result = graph.semantic_scores(graph.Graph(self.paths), "some query")
        return result, err.getvalue()


class TestAutostart(_Vault):
    def test_starts_ollama_and_goes_semantic(self):
        result, err = self._semantic([DOWN, DOWN, TAGS])
        self.assertIsNotNone(result)
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args.args[0], ["/bin/ollama", "serve"])
        self.assertEqual(err.count("started `ollama serve`"), 1)
        self.assertIn("embed", self.calls)

    def test_timeout_falls_back_with_unreachable_reason(self):
        result, _err = self._semantic([DOWN])
        self.assertIsNone(result)
        self.popen.assert_called_once()
        self.assertGreaterEqual(self.clock.now, graph.OLLAMA_START_TIMEOUT)
        self.assertEqual(graph.RETRIEVAL["reason"], "ollama unreachable")
        self.assertNotIn("embed", self.calls)

    def test_popen_failure_falls_back(self):
        self.popen.side_effect = OSError("no exec")
        result, _err = self._semantic([DOWN])
        self.assertIsNone(result)
        self.assertEqual(graph.RETRIEVAL["reason"], "ollama unreachable")

    def test_timeout_is_configurable(self):
        with mock.patch.object(graph, "OLLAMA_START_TIMEOUT", 2.0):
            self._semantic([DOWN])
        self.assertGreaterEqual(self.clock.now, 2.0)
        self.assertLess(self.clock.now, 5.0)

    def test_no_start_when_disabled(self):
        result, _err = self._semantic([DOWN], autostart="0")
        self.assertIsNone(result)
        self.popen.assert_not_called()

    def test_no_start_without_ollama_on_path(self):
        result, _err = self._semantic([DOWN], which=None)
        self.assertIsNone(result)
        self.popen.assert_not_called()

    def test_no_start_for_remote_url(self):
        result, _err = self._semantic([DOWN], url="http://gpu-box.example:11434")
        self.assertIsNone(result)
        self.popen.assert_not_called()

    def test_localhost_url_counts_as_local(self):
        result, _err = self._semantic([DOWN, TAGS], url="http://localhost:11434")
        self.assertIsNotNone(result)
        self.popen.assert_called_once()

    def test_no_start_when_model_missing(self):
        result, err = self._semantic([b'{"models": []}'])
        self.assertIsNone(result)
        self.assertIn("no bge-m3 model", err)
        self.popen.assert_not_called()

    def test_no_start_on_http_error(self):
        http = urllib.error.HTTPError("http://x/api/tags", 500, "boom", {}, BytesIO(b"x"))
        self._semantic([http])
        self.popen.assert_not_called()

    def test_no_start_when_already_running(self):
        result, _err = self._semantic([TAGS])
        self.assertIsNotNone(result)
        self.popen.assert_not_called()


class TestDetached(_Vault):
    def test_windows_flags(self):
        flags = {"DETACHED_PROCESS": 0x8, "CREATE_NEW_PROCESS_GROUP": 0x200,
                 "CREATE_NO_WINDOW": 0x08000000}
        with mock.patch.multiple(subprocess, create=True, **flags):
            self._semantic([DOWN, TAGS], os_name="win32")
        kwargs = self.popen.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], 0x8 | 0x200 | 0x08000000)
        self.assertNotIn("start_new_session", kwargs)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_posix_new_session(self):
        self._semantic([DOWN, TAGS], os_name="linux")
        kwargs = self.popen.call_args.kwargs
        self.assertIs(kwargs["start_new_session"], True)
        self.assertNotIn("creationflags", kwargs)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)


class TestDoctor(unittest.TestCase):
    def _run(self, autostart: str, installed: set[str]) -> str:
        home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {"PATH": os.environ.get("PATH", ""), "VAULT_OLLAMA_AUTOSTART": autostart}
        with mock.patch("onboarding._which",
                        side_effect=lambda t: f"/bin/{t}" if t in installed else None), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("pathlib.Path.home", return_value=home), \
             mock.patch("onboarding.urllib.request.urlopen", side_effect=OSError("down")), \
             redirect_stdout(StringIO()) as buf:
            with self.assertRaises(SystemExit):
                onboarding.cmd_doctor(Namespace())
        return buf.getvalue()

    def test_reports_autostart_on_and_ollama_found(self):
        out = self._run("1", {"ollama"})
        self.assertIn("Ollama autostart on", out)
        self.assertIn("`ollama` found", out)

    def test_reports_ollama_missing(self):
        out = self._run("1", set())
        self.assertIn("Ollama autostart on", out)
        self.assertIn("not found on PATH", out)

    def test_reports_autostart_off(self):
        out = self._run("0", {"ollama"})
        self.assertIn("Ollama autostart off", out)


if __name__ == "__main__":
    unittest.main()
