import unittest
from pathlib import Path

from _util import temporary_directory
from parley.store import Store
from parley import advisorchat


class TestChat(unittest.TestCase):
    def test_store_chat_history(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        proj.mkdir()
        s = Store(home=str(Path(tmp.name) / "home"))
        p = s.add_project("d", str(proj))
        s.add_chat(p["id"], "user", "hi")
        s.add_chat(p["id"], "advisor", "hello, I'm your advisor")
        ch = s.get_chat(p["id"])
        self.assertEqual([m["role"] for m in ch], ["user", "advisor"])
        # persists
        self.assertEqual(len(Store(home=str(Path(tmp.name) / "home")).get_chat(p["id"])), 2)

    def test_build_prompt_includes_history_and_message(self):
        hist = [{"role": "user", "text": "hi"}, {"role": "advisor", "text": "hello"}]
        p = advisorchat.build_prompt(hist, "what next?")
        self.assertIn("ADVISOR", p)
        self.assertIn("User: hi", p)
        self.assertIn("Advisor: hello", p)
        self.assertIn("User: what next?", p)
        self.assertTrue(p.rstrip().endswith("Advisor:"))

    def test_turn_prompt_reasserts_artifact_policy(self):
        p = advisorchat.turn_prompt("create domain-contract.md")
        self.assertIn("Artifact Draft", p)
        self.assertIn("USER MESSAGE", p)

    def test_parse_jsonl_completed(self):
        out = "\n".join([
            '{"type":"thread.started","thread_id":"019ea-XYZ"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Xin chào, tôi là advisor."}}',
            '{"type":"turn.completed"}',
        ])
        r = advisorchat.parse_jsonl(out)
        self.assertEqual(r["thread_id"], "019ea-XYZ")
        self.assertTrue(r["completed"])
        self.assertIn("advisor", r["reply"])

    def test_parse_jsonl_incomplete_is_not_completed(self):
        out = '{"type":"thread.started","thread_id":"t1"}\n{"type":"turn.started"}'
        r = advisorchat.parse_jsonl(out)
        self.assertEqual(r["thread_id"], "t1")
        self.assertFalse(r["completed"])      # chưa turn.completed -> timeout/error, không phải reply hợp lệ


if __name__ == "__main__":
    unittest.main()
