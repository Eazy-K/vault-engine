"""Tests for the personal-data guard and the engine leakcheck in tools/graph.py.

Standard library `unittest` only, matching graph.py's own constraint. Personal
data samples below are either synthetic (computed to pass/fail a checksum) or
well-known public test values (e.g. the card number is the standard Luhn test
number), never real people's data.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPH_PY = REPO_ROOT / "tools" / "graph.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import graph  # noqa: E402  (path must be set up first)


# --- synthetic personal-data samples -----------------------------------------

def _valid_tckn() -> str:
    """An 11-digit TCKN that satisfies the checksum (not a real ID)."""
    d = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    d9 = (sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10
    d10 = (sum(d) + d9) % 10
    return "".join(map(str, d + [d9, d10]))


def _invalid_tckn() -> str:
    s = _valid_tckn()
    return s[:-1] + str((int(s[-1]) + 1) % 10)


def _valid_iban() -> str:
    """A structurally valid TR IBAN (mod-97 checksum), not a real account."""
    bban22 = "0006123456789012345678"[:22]
    rearranged = bban22 + "TR" + "00"
    num = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - (int(num) % 97)
    return f"TR{check:02d}{bban22}"


VALID_CARD = "4111111111111111"  # guard:ignore -- standard public Luhn test number, not real


class TestGuardPatterns(unittest.TestCase):
    """scan_line() against each GUARD_PATTERNS entry."""

    def test_valid_tckn_flagged(self):
        self.assertIn("TCKN", graph.scan_line(f"kimlik no: {_valid_tckn()}"))

    def test_invalid_tckn_not_flagged(self):
        self.assertNotIn("TCKN", graph.scan_line(f"kimlik no: {_invalid_tckn()}"))

    def test_valid_iban_flagged(self):
        self.assertIn("IBAN", graph.scan_line(f"iban: {_valid_iban()}"))

    def test_invalid_iban_not_flagged(self):
        bad = _valid_iban()
        bad = bad[:4] + str((int(bad[4]) + 1) % 10) + bad[5:]
        self.assertNotIn("IBAN", graph.scan_line(f"iban: {bad}"))

    def test_luhn_card_flagged(self):
        self.assertIn("card number", graph.scan_line(f"card: {VALID_CARD}"))

    def test_non_luhn_digits_not_flagged(self):
        self.assertNotIn("card number", graph.scan_line("card: 1234567890123456"))

    def test_phone_flagged(self):
        self.assertIn("phone", graph.scan_line("call me at 0532 123 45 67"))  # guard:ignore

    def test_email_flagged(self):
        self.assertIn("email", graph.scan_line("contact someone@example.org.notreal"))  # guard:ignore

    def test_email_allowlist_not_flagged(self):
        self.assertNotIn("email", graph.scan_line("contact test@example.com"))

    def test_token_flagged(self):
        self.assertIn("token", graph.scan_line("key=AKIAABCDEFGHIJKLMNOP"))  # guard:ignore

    def test_password_flagged(self):
        self.assertIn("password", graph.scan_line("password: hunter22"))  # guard:ignore

    def test_password_placeholder_not_flagged(self):
        self.assertNotIn("password", graph.scan_line("password: <redacted>"))

    def test_guard_ignore_suppresses_all(self):
        line = f"kimlik no: {_valid_tckn()} guard:ignore"
        self.assertEqual(graph.scan_line(line), [])


class _TempRepo:
    """A throwaway git repo with a local (non-global) identity."""

    def __enter__(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._run(["git", "init", "-q"])
        self._run(["git", "config", "user.email", "tester@example.com"])
        self._run(["git", "config", "user.name", "Test Runner"])
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def _run(self, cmd, **kw):
        return subprocess.run(cmd, cwd=self.root, capture_output=True, text=True,
                               encoding="utf-8", check=True, **kw)

    def write(self, rel_path: str, content: str) -> Path:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def add(self, *rel_paths: str):
        self._run(["git", "add", *rel_paths])

    def graph_cli(self, *args, cwd: Path | None = None):
        return subprocess.run(
            [sys.executable, str(GRAPH_PY), *args],
            cwd=cwd or self.root, capture_output=True, text=True, encoding="utf-8")


class TestGuardStagedDiff(unittest.TestCase):
    def test_staged_diff_flags_from_repo_root(self):
        with _TempRepo() as repo:
            repo.write("note.txt", f"tckn: {_valid_tckn()}\n")
            repo.add("note.txt")
            result = repo.graph_cli("guard", "--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("possible TCKN", result.stdout)

    def test_staged_diff_runs_from_nested_cwd(self):
        # guard must resolve the repo root via git, not rely on process cwd
        # being the top level -- it should work the same from a subdirectory.
        with _TempRepo() as repo:
            repo.write("note.txt", f"tckn: {_valid_tckn()}\n")
            repo.add("note.txt")
            subdir = repo.root / "sub"
            subdir.mkdir()
            result = repo.graph_cli("guard", "--staged", cwd=subdir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("possible TCKN", result.stdout)

    def test_clean_diff_passes(self):
        with _TempRepo() as repo:
            repo.write("note.txt", "nothing sensitive here\n")
            repo.add("note.txt")
            result = repo.graph_cli("guard", "--staged")
            self.assertEqual(result.returncode, 0)
            self.assertIn("guard: clean", result.stdout)


class TestLeakcheckPaths(unittest.TestCase):
    def test_top_level_content_dir_blocked(self):
        self.assertTrue(graph._is_user_content_path("profile/me.md"))
        self.assertTrue(graph._is_user_content_path("notes/2024/x.md"))
        self.assertTrue(graph._is_user_content_path("vault.config.json"))

    def test_defaults_and_templates_allowed(self):
        self.assertFalse(graph._is_user_content_path("defaults/profile/example.md"))
        self.assertFalse(graph._is_user_content_path("templates/notes/example.md"))

    def test_unrelated_top_level_file_allowed(self):
        self.assertFalse(graph._is_user_content_path("README.md"))
        self.assertFalse(graph._is_user_content_path("notes.md"))  # a file, not the notes/ dir

    def test_leakcheck_cli_blocks_content_path(self):
        with _TempRepo() as repo:
            repo.write("profile/me.md", "hello\n")
            repo.add("profile/me.md")
            result = repo.graph_cli("leakcheck", "--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("profile/me.md", result.stdout)
            self.assertIn("user content path", result.stdout)

    def test_leakcheck_cli_allows_defaults_path(self):
        with _TempRepo() as repo:
            repo.write("defaults/profile/example.md", "hello\n")
            repo.add("defaults/profile/example.md")
            result = repo.graph_cli("leakcheck", "--staged")
            self.assertEqual(result.returncode, 0)


class TestLeakcheckIdentity(unittest.TestCase):
    """Home path / git identity / local denylist detection, and that the
    matched value itself is never echoed back."""

    FAKE_HOME = str(Path("C:/Users/exampleuser") if sys.platform == "win32"
                     else Path("/home/exampleuser"))

    def test_leak_terms_include_identity_and_home(self):
        with _TempRepo() as repo:
            from unittest import mock
            with mock.patch.object(graph.Path, "home", return_value=Path(self.FAKE_HOME)):
                terms = graph._leak_terms(repo.root)
            kinds = {kind for _, kind in terms}
            self.assertIn("home path", kinds)
            self.assertIn("git identity", kinds)
            values = {t for t, _ in terms}
            self.assertIn("tester@example.com", values)
            self.assertIn("Test Runner", values)

    def test_denylist_term_detected(self):
        with _TempRepo() as repo:
            git_dir = graph._git_dir(repo.root)
            (git_dir / "info").mkdir(parents=True, exist_ok=True)
            (git_dir / "info" / "vault-denylist").write_text(
                "# comment, skipped\nprojectcodename\n", encoding="utf-8")
            terms = graph._leak_terms(repo.root)
            self.assertIn(("projectcodename", "denylist term"), terms)

    def test_short_terms_skipped(self):
        with _TempRepo() as repo:
            repo._run(["git", "config", "user.name", "Al"])  # 2 chars, below the 4-char floor
            terms = graph._leak_terms(repo.root)
            self.assertNotIn("Al", {t for t, _ in terms})

    def test_git_identity_flagged_in_content_without_echoing_value(self):
        with _TempRepo() as repo:
            repo.write("note.txt", "written by Test Runner today\n")
            repo.add("note.txt")
            result = repo.graph_cli("leakcheck", "--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("possible git identity", result.stdout)
            self.assertNotIn("Test Runner", result.stdout)  # value itself must never be echoed

    def test_denylist_flagged_without_echoing_value(self):
        with _TempRepo() as repo:
            git_dir = graph._git_dir(repo.root)
            (git_dir / "info").mkdir(parents=True, exist_ok=True)
            (git_dir / "info" / "vault-denylist").write_text("projectcodename\n", encoding="utf-8")
            repo.write("note.txt", "this mentions projectcodename in passing\n")
            repo.add("note.txt")
            result = repo.graph_cli("leakcheck", "--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("possible denylist term", result.stdout)
            self.assertNotIn("projectcodename", result.stdout)

    def test_message_file_checked(self):
        with _TempRepo() as repo:
            msg = repo.write("msg.txt", "fix: mentions Test Runner in the message\n")
            result = repo.graph_cli("leakcheck", "--message-file", str(msg))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("possible git identity", result.stdout)
            self.assertNotIn("Test Runner", result.stdout)

    def test_guard_patterns_also_never_echo_value(self):
        with _TempRepo() as repo:
            repo.write("note.txt", f"tckn: {_valid_tckn()}\n")
            repo.add("note.txt")
            result = repo.graph_cli("leakcheck", "--staged")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(_valid_tckn(), result.stdout)


if __name__ == "__main__":
    unittest.main()
