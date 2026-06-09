import re
import sys
import time
import unittest

from parley.backends import CommandProfile, GenericCliBackend, SubprocessVerifyRunner


class TestBackend(unittest.TestCase):
    def test_run_once_stdin_roundtrip(self):
        b = GenericCliBackend()
        prof = CommandProfile([sys.executable, "-c",
                               "import sys;sys.stdout.write(sys.stdin.read().upper())"])
        res = b.run_once(prof, "hello parley", None, 30, 60)
        self.assertEqual(res.exit_code, 0)
        self.assertFalse(res.timed_out)
        self.assertIn("HELLO PARLEY", res.stdout)

    def test_run_once_stops_on_sentinel(self):
        b = GenericCliBackend()
        # prints the sentinel then sleeps 30s; stop_re must end the turn promptly
        code = ("import sys,time;"
                "print('<<<END nonce=\"zz\">>>', flush=True);"
                "time.sleep(30)")
        prof = CommandProfile([sys.executable, "-c", code])
        t0 = time.time()
        res = b.run_once(prof, "", None, 25, 60, stop_re=re.compile(r'<<<END nonce="zz">>>'))
        self.assertLess(time.time() - t0, 10)       # returned without waiting the 30s/idle
        self.assertFalse(res.timed_out)
        self.assertIn('<<<END nonce="zz">>>', res.stdout)

    def test_live_log_streams_pid_and_lines(self):
        # LS-019: when live_log is given, _spawn writes a pid header + every stdout
        # line in real time, then an end footer — so a watcher can tail "last line".
        from _util import temporary_directory
        from pathlib import Path
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        log = str(Path(tmp.name) / "live.log")
        b = GenericCliBackend()
        prof = CommandProfile([sys.executable, "-c",
                               "import sys;[print('line%d' % i, flush=True) for i in range(3)]"])
        res = b.run_once(prof, "", None, 30, 60, live_log=log)
        self.assertEqual(res.exit_code, 0)
        text = Path(log).read_text(encoding="utf-8")
        self.assertRegex(text, r"# pid=\d+")          # header carries the OS pid
        self.assertIn("line0", text)
        self.assertIn("line2", text)                  # every line streamed
        self.assertIn("# end exit=0", text)           # footer on clean exit

    def test_verify_pass_then_fail(self):
        vr = SubprocessVerifyRunner()
        ok = vr.run([[sys.executable, "-c", "raise SystemExit(0)"]], None, 30)
        self.assertEqual(ok.code, 0)
        bad = vr.run([[sys.executable, "-c", "raise SystemExit(0)"],
                      [sys.executable, "-c", "raise SystemExit(3)"]], None, 30)
        self.assertEqual(bad.code, 3)
        self.assertIsNotNone(bad.failed_gate)


if __name__ == "__main__":
    unittest.main()
