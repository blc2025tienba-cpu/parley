import unittest

from parley import context
from parley.protocol import Directive


class TestContext(unittest.TestCase):
    def test_executor_input_carries_report_protocol(self):
        d = Directive("DISPATCH", role="coder", slice="s1", prompt="implement X")
        out = context.executor_input(d, "CONTRACT", "/proj", "c.md",
                                     report_path="docs/reports/r.md")
        self.assertIn("implement X", out)
        self.assertIn("REPORT_PATH: docs/reports/r.md", out)
        self.assertIn('<<<REPORT path="docs/reports/r.md"', out)

    def test_executor_input_fallback_path(self):
        d = Directive("DISPATCH", role="reviewer", slice="s9", prompt="review")
        out = context.executor_input(d, "C")
        self.assertIn("slice-s9-reviewer", out)

    def test_executor_input_can_reference_prompt_file(self):
        d = Directive("DISPATCH", role="coder", slice="s1", prompt="very long private directive")
        out = context.executor_input(d, "C", "/proj", "contract.md", "reports/r.md",
                                     prompt_path="/data/prompts/task_1.md")
        self.assertIn("PROMPT_PATH: /data/prompts/task_1.md", out)
        self.assertNotIn("very long private directive", out)
        self.assertIn('<<<REPORT path="reports/r.md"', out)

    def test_role_prompt_document_contains_full_task(self):
        d = Directive("DISPATCH", role="coder", slice="s1", prompt="implement X")
        out = context.role_prompt_document(d, "CONTRACT BODY", "/proj", "contract.md",
                                           "reports/r.md", "task_1")
        self.assertIn("implement X", out)
        self.assertIn("CONTRACT BODY", out)
        self.assertIn("TASK_ID: task_1", out)

    def test_advisor_seed_has_nonce_and_directives(self):
        seed = context.advisor_seed("goal", None, 12)
        self.assertIn("{N}", seed)
        self.assertIn("DISPATCH", seed)
        self.assertIn("(chua co)", seed)   # null contract -> bootstrap hint

    # ---- ADR-14 warm delta builders: must OMIT header/policy, keep nonce ----
    def test_followup_delta_omits_header_keeps_nonce(self):
        report = {"slice": "s1", "role": "coder", "done": True, "verdict": None,
                  "report_path": "r.md", "excerpt": "did the work", "diff": ""}
        delta = context.advisor_followup_delta(["- step 1"], report)
        full = context.advisor_followup("GOALX", "CONTRACTX", 3, ["- step 1"], report)
        # delta carries the new report + nonce contract...
        self.assertIn("REPORT MOI", delta)
        self.assertIn("{N}", delta)
        self.assertIn('review="APPROVE|REJECT"', delta)
        # ...but NOT the re-seeded goal/contract/full policy (session remembers them)
        self.assertNotIn("GOALX", delta)
        self.assertNotIn("CONTRACTX", delta)
        self.assertNotIn("# CACH LAM VIEC", delta)
        # full seed is much larger than the delta (token saving is the whole point)
        self.assertLess(len(delta), len(full))

    def test_verify_delta_omits_header(self):
        delta = context.advisor_verify_delta(["- p"], {"exit": 1, "failed_gate": "npm test", "tail": "boom"})
        self.assertIn("KET QUA VERIFY", delta)
        self.assertIn("{N}", delta)
        self.assertNotIn("# CACH LAM VIEC", delta)

    def test_reject_delta_omits_header(self):
        delta = context.advisor_reject_delta([], "NONE")
        self.assertIn("LOI LUOT TRUOC", delta)
        self.assertIn("{N}", delta)
        self.assertNotIn("# CACH LAM VIEC", delta)


if __name__ == "__main__":
    unittest.main()
