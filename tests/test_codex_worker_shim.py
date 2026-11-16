"""codex-worker and the npm `.cmd` shim: on Windows the shim is bypassed (node + the JS entry
run directly) so cmd.exe never sees `>`, `&`, `|`, `%` in the arguments."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import GRAPH_PATH, graph  # noqa: E402,F401

sys.path.insert(0, str(GRAPH_PATH.parent))
import codex_worker  # noqa: E402

NPM_SHIM = ('@ECHO off\r\nGOTO start\r\n:start\r\nSETLOCAL\r\nCALL :find_dp0\r\n\r\n'
            'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
            '  SET "_prog=node"\r\n)\r\n\r\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
            '"%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
NASTY = "run `python -m unittest -q > test.log 2>&1` & echo %PATH% | more"
FAKE_JS = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
print(json.dumps({"type": "item.completed", "args": sys.argv[1:]}))
'''


def make_shim(root: Path, entry: bool = True, node_exe: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    js = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if entry:
        js.parent.mkdir(parents=True)
        js.write_text(FAKE_JS, encoding="utf-8")
    if node_exe:
        (root / "node.exe").write_bytes(b"")
    shim = root / "codex.cmd"
    shim.write_bytes(NPM_SHIM.encode("utf-8"))
    return shim


class ResolveShimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_node_from_path_without_sibling_node_exe(self):
        shim = make_shim(self.root)
        node, entry = codex_worker.resolve_shim(str(shim), which=lambda _n: "/usr/bin/node")
        self.assertEqual(node, "/usr/bin/node")
        self.assertEqual(Path(entry), self.root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")

    def test_sibling_node_exe_wins(self):
        shim = make_shim(self.root, node_exe=True)
        node, _ = codex_worker.resolve_shim(str(shim), which=lambda _n: "/usr/bin/node")
        self.assertEqual(Path(node), self.root / "node.exe")

    def test_entry_path_is_read_from_the_shim_text(self):
        (self.root / "lib").mkdir()
        (self.root / "lib" / "cli.js").write_text("", encoding="utf-8")
        shim = self.root / "codex.cmd"
        shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\lib\\cli.js" %*\r\n', encoding="utf-8")
        _, entry = codex_worker.resolve_shim(str(shim), which=lambda _n: "node")
        self.assertEqual(Path(entry), self.root / "lib" / "cli.js")

    def test_missing_entry_raises_with_way_out(self):
        shim = make_shim(self.root, entry=False)
        with self.assertRaises(codex_worker.WorkerError) as cm:
            codex_worker.resolve_shim(str(shim), which=lambda _n: "node")
        self.assertIn("JavaScript entry", str(cm.exception))
        self.assertIn("--codex", str(cm.exception))
        self.assertIn("VAULT_CODEX_BIN", str(cm.exception))

    def test_missing_node_raises(self):
        shim = make_shim(self.root)
        with self.assertRaises(codex_worker.WorkerError) as cm:
            codex_worker.resolve_shim(str(shim), which=lambda _n: None)
        self.assertIn("node", str(cm.exception))
        self.assertIn("VAULT_CODEX_BIN", str(cm.exception))

    def test_unreadable_shim_raises(self):
        with self.assertRaises(codex_worker.WorkerError):
            codex_worker.resolve_shim(str(self.root / "nope.cmd"))


@unittest.skipUnless(os.name == "nt", "the .cmd shim is bypassed on Windows only")
class CmdShimBypassTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.launch = self.root / "launch"
        self.launch.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        self.shim = make_shim(self.root / "npm")
        old = os.getcwd()
        os.chdir(self.launch)
        self.addCleanup(os.chdir, old)

    def _which(self, name, *a, **k):
        return sys.executable if name == "node" else str(self.shim)

    def _argv(self):
        with mock.patch("codex_worker.shutil.which", side_effect=self._which):
            return codex_worker.codex_argv(str(self.shim), "/w", str(self.out / "last.md"),
                                           "none", "m", "low", NASTY)

    def test_argv_never_starts_with_a_shim(self):
        argv = self._argv()
        self.assertFalse(argv[0].lower().endswith((".cmd", ".bat")), argv)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("codex.js"))

    def test_events_land_in_events_jsonl_and_no_stray_file(self):
        argv = self._argv()
        rc, timed_out = codex_worker.run_codex(argv, "prompt", self.out, 60)
        self.assertEqual((rc, timed_out), (0, False))
        events = (self.out / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("thread.started", events)
        self.assertIn("%PATH%", events)
        self.assertEqual(os.listdir(self.launch), [])


if __name__ == "__main__":
    unittest.main()
