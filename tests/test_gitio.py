import subprocess
import tempfile
import unittest
from pathlib import Path

from _util import temporary_directory
from parley import gitio


def _has_git():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git not available")
class TestGitio(unittest.TestCase):
    def test_commit_slice_returns_sha(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=d, capture_output=True)
        (Path(d) / "f.txt").write_text("hello", encoding="utf-8")
        sha = gitio.commit_slice(d, "s1")
        self.assertTrue(sha and len(sha) >= 7)
        log = subprocess.run(["git", "log", "--oneline"], cwd=d, capture_output=True, text=True)
        self.assertIn("parley: slice s1", log.stdout)


if __name__ == "__main__":
    unittest.main()
