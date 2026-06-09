import unittest
from dataclasses import dataclass

from parley.fsm import Fsm
from parley import supervisor


@dataclass
class D:
    role: str
    slice: str


class TestFsm(unittest.TestCase):
    def test_first_dispatch_ok_sets_current(self):
        f = Fsm()
        self.assertTrue(f.allow(D("coder", "s1")))
        self.assertEqual(f.current, "s1")

    def test_cannot_move_slice_until_done(self):
        f = Fsm()
        f.allow(D("coder", "s1"))
        self.assertFalse(f.allow(D("coder", "s2")))      # s1 not approve+verify
        self.assertIn("chua APPROVE", f.why)
        f.observe({"slice": "s1", "verdict": "APPROVE"})
        f.observe_verify("s1", 0)
        self.assertTrue(f.allow(D("coder", "s2")))       # now allowed

    def test_reject_forces_fixer(self):
        f = Fsm()
        f.allow(D("coder", "s1"))
        f.observe({"slice": "s1", "verdict": "REJECT"})
        self.assertFalse(f.allow(D("coder", "s1")))      # non-fixer blocked
        self.assertTrue(f.allow(D("fixer", "s1")))       # fixer allowed

    def test_slice_done_idempotent(self):
        f = Fsm()
        f.observe({"slice": "s1", "verdict": "APPROVE"})
        f.observe_verify("s1", 0)
        self.assertTrue(f.slice_done("s1"))
        self.assertFalse(f.slice_done("s1"))             # only once -> single commit

    def test_verify_fail_blocks_done(self):
        f = Fsm()
        f.observe({"slice": "s1", "verdict": "APPROVE"})
        f.observe_verify("s1", 1)
        self.assertFalse(f.slice_done("s1"))


class TestSupervisor(unittest.TestCase):
    def test_parse_valid(self):
        d = supervisor.parse_decision('noise {"verdict":"steer","reason":"r","inject":"i"} tail')
        self.assertEqual((d.verdict, d.inject), ("steer", "i"))

    def test_parse_bad_defaults_continue(self):
        self.assertEqual(supervisor.parse_decision("no json here").verdict, "continue")
        self.assertEqual(supervisor.parse_decision('{"verdict":"bogus"}').verdict, "continue")

    def test_parse_strips_ansi_and_decoration(self):
        raw = ('\x1b[38;5;141m> \x1b[0m{"verdict":"stop","reason":"off goal","inject":""}\n'
               '\x1b[38;5;8m Credits: 0.01 \x1b[0m')
        self.assertEqual(supervisor.parse_decision(raw).verdict, "stop")

    def test_parse_prefers_answer_over_echoed_example(self):
        # an echoed schema example appears first; the real answer (last) must win
        raw = ('schema: {"verdict":"continue|steer|stop","reason":"...","inject":"..."}\n'
               '{"verdict":"stop","reason":"unsafe","inject":""}')
        self.assertEqual(supervisor.parse_decision(raw).verdict, "stop")

    def test_gate_uses_runner(self):
        d = supervisor.gate("g", "c", "intent", [], lambda p: '{"verdict":"stop","reason":"x"}')
        self.assertEqual(d.verdict, "stop")


if __name__ == "__main__":
    unittest.main()
