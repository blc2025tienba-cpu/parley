import unittest

from parley.tasks import project_tasks


class TestProjectTasks(unittest.TestCase):
    def test_happy_path_done(self):
        events = [
            {"type": "dispatch", "task_id": "t1", "role": "coder", "slice": "s1",
             "title": "build it", "origin": "planned", "turn": 0},
            {"type": "report", "task_id": "t1", "slice": "s1", "done": True, "verdict": None},
            {"type": "advisor", "reviews_task": "t1", "verdict": "APPROVE"},
            {"type": "verify", "slice": "s1", "exit": 0},
            {"type": "slice_done", "task_id": "t1", "slice": "s1", "commit": "abc1234"},
        ]
        tasks = project_tasks(events)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual((t["id"], t["status"], t["origin"]), ("t1", "done", "planned"))
        self.assertEqual(t["commit"], "abc1234")
        self.assertEqual(t["title"], "build it")

    def test_reject_spawns_emergent_child(self):
        events = [
            {"type": "dispatch", "task_id": "t1", "role": "coder", "slice": "s1", "origin": "planned"},
            {"type": "report", "task_id": "t1", "slice": "s1", "done": True},
            {"type": "advisor", "reviews_task": "t1", "verdict": "REJECT"},
            {"type": "dispatch", "task_id": "t2", "role": "fixer", "slice": "s1",
             "origin": "emergent", "parent_task_id": "t1"},
            {"type": "report", "task_id": "t2", "slice": "s1", "done": True},
            {"type": "advisor", "reviews_task": "t2", "verdict": "APPROVE"},
            {"type": "slice_done", "task_id": "t2", "slice": "s1", "commit": "def5678"},
        ]
        tasks = project_tasks(events)
        self.assertEqual([t["id"] for t in tasks], ["t1", "t2"])
        self.assertEqual(tasks[0]["status"], "rejected")
        self.assertEqual((tasks[1]["origin"], tasks[1]["parent_task_id"]), ("emergent", "t1"))
        self.assertEqual(tasks[1]["status"], "done")

    def test_running_and_awaiting(self):
        events = [
            {"type": "dispatch", "task_id": "t1", "role": "analyzer", "slice": "s1"},
        ]
        self.assertEqual(project_tasks(events)[0]["status"], "running")
        events.append({"type": "report", "task_id": "t1", "slice": "s1", "done": True})
        self.assertEqual(project_tasks(events)[0]["status"], "awaiting_advisor_review")

    def test_tolerates_missing_task_id(self):
        events = [
            {"type": "dispatch", "role": "coder", "slice": "s9", "turn": 3},
            {"type": "slice_done", "slice": "s9", "commit": "z"},
        ]
        t = project_tasks(events)[0]
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["id"], "s9@t3")


if __name__ == "__main__":
    unittest.main()
