import unittest

from parley import housekeeping as hk
from parley.backends import RunResult
from parley.config import Config, Housekeeping, Role


class FakeBackend:
    def __init__(self, out):
        self.out = out
        self.calls = []

    def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
        self.calls.append((profile.cmd, prompt))
        return RunResult(self.out, 0)


def _cfg(enabled=True, model="mini"):
    return Config(project_dir="/p",
                  roles={"coder": Role(cmd=["opencode", "run", "--dangerously-skip-permissions"], edit=True)},
                  housekeeping=Housekeeping(enabled=enabled, model=model, from_role="coder"))


class TestHousekeeping(unittest.TestCase):
    def test_profile_appends_minimal_model(self):
        prof = hk.hk_profile(_cfg(model="mini"))
        self.assertEqual(prof.cmd[-2:], ["--model", "mini"])
        self.assertEqual(prof.cmd[:2], ["opencode", "run"])

    def test_suggest_message_parses_clean_line(self):
        be = FakeBackend("> build \u00b7 mini\n\nfeat: add smoke result\n")
        msg = hk.suggest_commit_message(_cfg(), be, "s1", "diff text", "fallback")
        self.assertEqual(msg, "feat: add smoke result")

    def test_suggest_message_fallback_when_disabled_or_no_diff(self):
        self.assertEqual(hk.suggest_commit_message(_cfg(enabled=False), FakeBackend("x"),
                                                   "s1", "d", "fb"), "fb")
        self.assertEqual(hk.suggest_commit_message(_cfg(), FakeBackend("x"),
                                                   "s1", "", "fb"), "fb")

    def test_update_changelog_runs_when_enabled(self):
        be = FakeBackend("done")
        self.assertTrue(hk.update_changelog(_cfg(), be, "s1", "did stuff"))
        self.assertEqual(len(be.calls), 1)
        self.assertFalse(hk.update_changelog(_cfg(enabled=False), be, "s1", "x"))


if __name__ == "__main__":
    unittest.main()
