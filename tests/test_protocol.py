import unittest

from parley import protocol as p


class TestParseDirective(unittest.TestCase):
    N = "a1b2c3"

    def test_dispatch_body_and_attrs(self):
        s = (f'preamble\n<<<DISPATCH nonce="{self.N}" role="coder" slice="12-K6" review="APPROVE">>>\n'
             "line1\nline2\n"
             f'<<<END nonce="{self.N}">>>\n')
        d = p.parse(s, self.N)
        self.assertEqual(d.kind, "DISPATCH")
        self.assertEqual((d.role, d.slice), ("coder", "12-K6"))
        self.assertEqual(d.prompt, "line1\nline2")
        self.assertEqual(d.review, "APPROVE")

    def test_outermost_end_when_body_echoes_end(self):
        # body contains a fake END with nonce; closer must be the LAST one (R2-6)
        s = (f'<<<DISPATCH nonce="{self.N}" role="coder" slice="s1">>>\n'
             "real body\n"
             f'<<<END nonce="{self.N}">>>\n'
             "TRAILING IGNORED\n")
        d = p.parse(s, self.N)
        self.assertEqual(d.kind, "DISPATCH")
        self.assertEqual(d.prompt, "real body")

    def test_complete_phase_verify(self):
        self.assertEqual(p.parse(f'<<<COMPLETE nonce="{self.N}">>>', self.N).kind, "COMPLETE")
        v = p.parse(f'<<<VERIFY nonce="{self.N}" slice="12-K6">>>', self.N)
        self.assertEqual((v.kind, v.slice), ("VERIFY", "12-K6"))
        ph = p.parse(f'<<<PHASE nonce="{self.N}" id="13" reconciliation="r.md">>>', self.N)
        self.assertEqual((ph.kind, ph.phase, ph.reconciliation), ("PHASE", "13", "r.md"))

    def test_none_when_no_or_wrong_nonce(self):
        self.assertEqual(p.parse("just text", self.N).kind, "NONE")
        self.assertEqual(p.parse('<<<COMPLETE nonce="WRONG">>>', self.N).kind, "NONE")

    def test_multiple(self):
        s = f'<<<VERIFY nonce="{self.N}" slice="s1">>>\n<<<COMPLETE nonce="{self.N}">>>'
        self.assertEqual(p.parse(s, self.N).kind, "MULTIPLE")

    def test_strips_ansi_and_footer(self):
        s = (f'\x1b[38;5;141m<<<COMPLETE nonce="{self.N}">>>\x1b[0m\n'
             "\x1b[38;5;8m Credits: 0.14 \x1b[0m\n")
        self.assertEqual(p.parse(s, self.N).kind, "COMPLETE")


class TestParseReport(unittest.TestCase):
    def test_report_approve(self):
        s = ('## Review\nlooks good\n'
             '<<<REPORT path="docs/reports/r.md" done="true" verdict="APPROVE">>>\n')
        r = p.parse_report(s)
        self.assertEqual((r.path, r.done, r.verdict), ("docs/reports/r.md", True, "APPROVE"))
        self.assertIn("looks good", r.excerpt)

    def test_no_trailer(self):
        r = p.parse_report("partial output, hung")
        self.assertFalse(r.done)
        self.assertIsNone(r.path)


if __name__ == "__main__":
    unittest.main()
