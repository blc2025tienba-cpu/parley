import unittest

from parley import planner


class TestPlanner(unittest.TestCase):
    def test_parse_goals_str_and_obj(self):
        raw = ('> planner\n{"execution_mode":"parallel",'
               '"goals":["build api",{"title":"write tests"},{"name":"deploy"}],'
               '"reason":"independent"}')
        p = planner.parse_plan(raw)
        self.assertEqual(p["execution_mode"], "parallel")
        self.assertEqual([g["title"] for g in p["goals"]], ["build api", "write tests", "deploy"])
        self.assertEqual(p["goals"][1]["description"], "")
        self.assertEqual(p["reason"], "independent")

    def test_invalid_mode_defaults_sequential(self):
        p = planner.parse_plan('{"execution_mode":"weird","goals":[{"title":"x"}]}')
        self.assertEqual(p["execution_mode"], "sequential")

    def test_fallback_on_bad_or_no_json(self):
        self.assertEqual(planner.parse_plan("no json").goals if False else
                         planner.parse_plan("no json")["goals"], [])
        self.assertEqual(planner.parse_plan('{bad}')["execution_mode"], "sequential")

    def test_parse_markdown_task_plan(self):
        raw = """
## 4. Task List

## Task 0 - Confirm Domain Contract
body

```text
## Task 99 - Ignored In Fence
```

## Task 1 - Builder Discovery
"""
        p = planner.parse_plan(raw)
        self.assertEqual([g["title"] for g in p["goals"]], [
            "Task 0 - Confirm Domain Contract",
            "Task 1 - Builder Discovery",
        ])
        self.assertIn("body", p["goals"][0]["description"])
        self.assertEqual(p["reason"], "parsed from markdown task headings")

    def test_parse_markdown_phase_plan(self):
        p = planner.parse_plan("### P1. Builder Discovery\n### P2. MCP Review")
        self.assertEqual([g["title"] for g in p["goals"]], [
            "P1 - Builder Discovery",
            "P2 - MCP Review",
        ])

    def test_parse_markdown_plan_wrapped_in_code_fence(self):
        p = planner.parse_plan("```md\n## Task 0 - Confirm Domain Contract\n## Task 1 - Builder Discovery\n```")
        self.assertEqual([g["title"] for g in p["goals"]], [
            "Task 0 - Confirm Domain Contract",
            "Task 1 - Builder Discovery",
        ])

    def test_plan_uses_runner(self):
        p = planner.plan(["idea A", "idea B"],
                         lambda prompt: '{"execution_mode":"sequential","goals":[{"title":"do A"}],"reason":"r"}')
        self.assertEqual(p["goals"], [{"title": "do A", "description": ""}])
        self.assertEqual(p["reason"], "r")


if __name__ == "__main__":
    unittest.main()
