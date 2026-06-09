import json
import sys
import time
import unittest
from pathlib import Path

from _util import temporary_directory
from parley.store import Store
from parley.manager import RunManager


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        (self.proj / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self.store = Store(home=str(self.home))

    def test_project_and_goal_crud(self):
        p = self.store.add_project("demo", str(self.proj))
        self.assertEqual(self.store.get_project(p["id"])["name"], "demo")

        g = self.store.add_goal(p["id"], "do X")
        self.assertEqual(g["state"], "idle")
        self.assertEqual(g["project_id"], p["id"])

        # per-goal config + correct content
        cfg = self.store.read_config(g["id"])
        self.assertEqual(cfg["goal"], "do X")
        self.assertEqual(Path(cfg["project_dir"]), self.proj.resolve())
        self.assertIn(["cargo", "test"], cfg["verify"]["gates"])
        # per-goal data dir under home/data/<gid>
        self.assertEqual(Path(cfg["data_dir"]), (self.home / "data" / g["id"]))

    def test_edit_goal_propagates_to_config(self):
        p = self.store.add_project("d", str(self.proj))
        g = self.store.add_goal(p["id"], "old")
        self.store.update_goal(g["id"], goal="new")
        self.assertEqual(self.store.read_config(g["id"])["goal"], "new")

    def test_edit_goal_description_rebuilds_config(self):
        p = self.store.add_project("d", str(self.proj))
        g = self.store.add_goal(p["id"], "title", "first scope")
        self.assertIn("# Goal Details\nfirst scope", self.store.read_config(g["id"])["goal"])
        self.store.update_goal(g["id"], description="second scope")
        cfg = self.store.read_config(g["id"])
        self.assertIn("# Goal Details\nsecond scope", cfg["goal"])
        self.assertNotIn("first scope", cfg["goal"])

    def test_delete_goal_is_soft_and_keeps_entry(self):
        p = self.store.add_project("d", str(self.proj))
        g = self.store.add_goal(p["id"], "to delete")
        deleted = self.store.delete_goal(g["id"])
        self.assertEqual(deleted["state"], "deleted")
        self.assertIn("deleted_at", deleted)
        # entry + data dir survive for audit
        self.assertIsNotNone(self.store.get_goal(g["id"]))
        self.assertTrue(Path(g["data_dir"]).exists())

    def test_delete_running_goal_is_refused(self):
        p = self.store.add_project("d", str(self.proj))
        g = self.store.add_goal(p["id"], "busy")
        self.store.update_goal(g["id"], state="running")
        with self.assertRaisesRegex(ValueError, "running"):
            self.store.delete_goal(g["id"])
        self.assertEqual(self.store.get_goal(g["id"])["state"], "running")

    def test_plan_and_approve_contract(self):
        p = self.store.add_project("d", str(self.proj))
        runner = lambda prompt: ('{"execution_mode":"sequential",'
                                 '"goals":[{"title":"goal one","description":"scope one"},'
                                 '{"title":"goal two"}],"reason":"seq"}')
        c = self.store.plan_project(p["id"], ["idea x"], runner)
        self.assertEqual(c["execution_mode"], "sequential")
        self.assertEqual(len(c["goals"]), 2)
        self.assertEqual(c["goals"][0]["description"], "scope one")
        self.assertFalse(c["approved"])
        created = self.store.approve_contract(p["id"])
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0]["description"], "scope one")
        self.assertIn("# Goal Details\nscope one", self.store.read_config(created[0]["id"])["goal"])
        self.assertEqual(len(self.store.list_goals(p["id"])), 2)
        self.assertTrue(self.store.get_project(p["id"])["contract"]["approved"])
        self.assertEqual(self.store.approve_contract(p["id"]), [])   # idempotent

    def test_replace_approved_contract_supersedes_unstarted_goals(self):
        p = self.store.add_project("d", str(self.proj))
        self.store.set_contract_draft(p["id"], {
            "execution_mode": "sequential",
            "goals": [{"title": "old one"}, {"title": "old two"}],
            "reason": "old",
        })
        old = self.store.approve_contract(p["id"])
        self.store.update_goal(old[0]["id"], state="done")

        draft = self.store.set_contract_draft(p["id"], {
            "execution_mode": "sequential",
            "goals": [{"title": "new one"}, {"title": "new two"}],
            "reason": "new",
        })
        self.assertFalse(draft["approved"])
        self.assertEqual(draft["base_goal_ids"], [g["id"] for g in old])
        new = self.store.approve_contract(p["id"], strategy="replace")

        self.assertEqual(self.store.get_goal(old[0]["id"])["state"], "done")
        self.assertEqual(self.store.get_goal(old[1]["id"])["state"], "superseded")
        self.assertEqual(self.store.get_project(p["id"])["contract"]["goal_ids"], [g["id"] for g in new])
        self.assertEqual(len(self.store.get_project(p["id"])["contract_history"]), 1)

    def test_append_approved_contract_keeps_existing_goal_ids(self):
        p = self.store.add_project("d", str(self.proj))
        self.store.set_contract_draft(p["id"], {
            "goals": [{"title": "old"}], "reason": "old",
        })
        old = self.store.approve_contract(p["id"])
        self.store.set_contract_draft(p["id"], {
            "goals": [{"title": "new"}], "reason": "new",
        })
        new = self.store.approve_contract(p["id"], strategy="append")
        self.assertEqual(
            self.store.get_project(p["id"])["contract"]["goal_ids"],
            [old[0]["id"], new[0]["id"]],
        )

    def test_append_contract_skips_duplicate_goal_titles(self):
        p = self.store.add_project("d", str(self.proj))
        self.store.set_contract_draft(p["id"], {
            "goals": [{"title": "old"}], "reason": "old",
        })
        old = self.store.approve_contract(p["id"])
        self.store.set_contract_draft(p["id"], {
            "goals": [{"title": "old"}, {"title": "new"}], "reason": "mixed",
        })
        new = self.store.approve_contract(p["id"], strategy="append")
        self.assertEqual([g["goal"] for g in new], ["new"])
        self.assertEqual(
            self.store.get_project(p["id"])["contract"]["goal_ids"],
            [old[0]["id"], new[0]["id"]],
        )

    def test_save_artifact_writes_inside_project_only(self):
        p = self.store.add_project("d", str(self.proj))
        r = self.store.save_artifact(p["id"], "docs/parley/domain-contract.md", "# Domain")
        self.assertEqual(r["path"], "docs/parley/domain-contract.md")
        self.assertEqual((self.proj / "docs" / "parley" / "domain-contract.md").read_text(encoding="utf-8"), "# Domain")
        self.assertEqual(self.store.get_project(p["id"])["contract_path"],
                         str((self.proj / "docs" / "parley" / "domain-contract.md").resolve()))
        g = self.store.add_goal(p["id"], "uses contract")
        self.assertEqual(self.store.read_config(g["id"])["contract_path"],
                         str((self.proj / "docs" / "parley" / "domain-contract.md").resolve()))
        with self.assertRaises(ValueError):
            self.store.save_artifact(p["id"], "../x.md", "bad")
        with self.assertRaises(ValueError):
            self.store.save_artifact(p["id"], str(Path(self.tmp.name) / "x.md"), "bad")
        with self.assertRaises(ValueError):
            self.store.save_artifact(p["id"], "docs/run.exe", "bad")

    def test_reload_migrates_recorded_domain_contract_into_goal_config(self):
        p = self.store.add_project("d", str(self.proj))
        g = self.store.add_goal(p["id"], "legacy goal")
        target = self.proj / "docs" / "d" / "domain-contract.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Domain", encoding="utf-8")
        self.store.update_project(p["id"], artifacts=[{"path": "docs/d/domain-contract.md"}])

        migrated = Store(home=str(self.home))
        self.assertEqual(migrated.get_project(p["id"])["contract_path"], str(target.resolve()))
        self.assertEqual(migrated.read_config(g["id"])["contract_path"], str(target.resolve()))

    def test_init_project_caches_gates_and_goal_is_cheap(self):
        p = self.store.add_project("d", str(self.proj))
        self.assertFalse(p.get("initialized"))
        self.store.add_goal(p["id"], "g1")             # lazy project-init lần đầu
        p2 = self.store.get_project(p["id"])
        self.assertTrue(p2["initialized"])
        self.assertIn(["cargo", "test"], p2["gates"])  # gates cache ở project
        # goal kế dùng gates cache (vẫn ra config đúng)
        g2 = self.store.add_goal(p["id"], "g2")
        self.assertIn(["cargo", "test"], self.store.read_config(g2["id"])["verify"]["gates"])

    def test_persists_across_reload(self):
        p = self.store.add_project("d", str(self.proj))
        self.store.add_goal(p["id"], "g1")
        self.store.add_goal(p["id"], "g2")
        s2 = Store(home=str(self.home))
        self.assertEqual(len(s2.list_projects()), 1)
        self.assertEqual(len(s2.list_goals(p["id"])), 2)


class TestRunManager(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        self.store = Store(home=str(Path(self.tmp.name) / "home"))
        p = self.store.add_project("d", str(proj))
        self.g = self.store.add_goal(p["id"], "g")
        self.mgr = RunManager(self.store)

    def test_start_then_force_stop(self):
        self.mgr.start(self.g["id"], command=[sys.executable, "-c", "import time;time.sleep(30)"])
        self.assertTrue(self.mgr.is_running(self.g["id"]))
        self.assertEqual(self.store.get_goal(self.g["id"])["state"], "running")
        self.assertIsNotNone(self.store.get_goal(self.g["id"])["pid"])

        self.mgr.stop(self.g["id"], force=True)
        time.sleep(0.6)
        self.assertFalse(self.mgr.is_running(self.g["id"]))
        self.assertEqual(self.store.get_goal(self.g["id"])["state"], "stopped")
        # graceful control signal also written
        ctl = json.loads((Path(self.g["data_dir"]) / "control.json").read_text(encoding="utf-8"))
        self.assertEqual(ctl["verdict"], "stop")

    def test_control_writes_steer(self):
        self.mgr.control(self.g["id"], "steer", inject="ưu tiên test")
        ctl = json.loads((Path(self.g["data_dir"]) / "control.json").read_text(encoding="utf-8"))
        self.assertEqual((ctl["verdict"], ctl["inject"]), ("steer", "ưu tiên test"))


class TestSequentialProjectRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        self.marker = Path(self.tmp.name) / "order.txt"
        self.store = Store(home=str(self.home))
        self.project = self.store.add_project("seq", str(self.proj))
        self.store.plan_project(
            self.project["id"], ["two goals"],
            lambda prompt: ('{"execution_mode":"sequential","goals":'
                            '[{"title":"one"},{"title":"two"}],"reason":"ordered"}'),
        )
        self.goals = self.store.approve_contract(self.project["id"])

    def _command_factory(self, reasons):
        def command(goal):
            reason = reasons[goal["id"]]
            conv = Path(goal["data_dir"]) / "conversation.ndjson"
            script = (
                "from pathlib import Path; import json; "
                f"m=Path({str(self.marker)!r}); m.parent.mkdir(parents=True,exist_ok=True); "
                f"m.open('a',encoding='utf-8').write({goal['id']!r}+'\\n'); "
                f"c=Path({str(conv)!r}); c.parent.mkdir(parents=True,exist_ok=True); "
                f"c.open('a',encoding='utf-8').write(json.dumps({{'type':'stopped','reason':{reason!r}}})+'\\n')"
            )
            return [sys.executable, "-c", script]
        return command

    def _wait_terminal(self, manager, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            state = manager.project_status(self.project["id"])["state"]
            if state != "running":
                return state
            time.sleep(0.05)
        self.fail("project runner did not reach terminal state")

    def test_runs_contract_goals_in_order(self):
        reasons = {g["id"]: "done" for g in self.goals}
        manager = RunManager(self.store, command_factory=self._command_factory(reasons),
                             poll_interval=0.02, autostart=False)
        run = manager.start_project(self.project["id"])
        self.assertEqual(run["active_goal_id"], self.goals[0]["id"])
        self.assertEqual(self._wait_terminal(manager), "done")
        self.assertEqual(self.marker.read_text(encoding="utf-8").splitlines(),
                         [self.goals[0]["id"], self.goals[1]["id"]])
        self.assertEqual([self.store.get_goal(g["id"])["state"] for g in self.goals],
                         ["done", "done"])

    def test_failure_blocks_remaining_goal(self):
        reasons = {self.goals[0]["id"]: "gov", self.goals[1]["id"]: "done"}
        manager = RunManager(self.store, command_factory=self._command_factory(reasons),
                             poll_interval=0.02, autostart=False)
        manager.start_project(self.project["id"])
        self.assertEqual(self._wait_terminal(manager), "blocked")
        self.assertEqual(self.marker.read_text(encoding="utf-8").splitlines(), [self.goals[0]["id"]])
        self.assertEqual(self.store.get_goal(self.goals[0]["id"])["state"], "failed")
        self.assertEqual(self.store.get_goal(self.goals[1]["id"])["state"], "queued")

    def test_resume_requeues_blocked_goal_and_completes(self):
        # Goal 0 blocks (gov stop) -> project blocked; after fixing the cause we
        # flip its scripted reason to done, resume, and the runner should re-queue
        # it, finish it, then run goal 1 to completion.
        reasons = {self.goals[0]["id"]: "gov", self.goals[1]["id"]: "done"}
        manager = RunManager(self.store, command_factory=self._command_factory(reasons),
                             poll_interval=0.02, autostart=False)
        manager.start_project(self.project["id"])
        self.assertEqual(self._wait_terminal(manager), "blocked")
        self.assertEqual(self.store.get_goal(self.goals[0]["id"])["state"], "failed")
        # operator "fixes" the blocking goal so the next run succeeds
        reasons[self.goals[0]["id"]] = "done"
        run = manager.resume_project(self.project["id"])
        self.assertEqual(run["state"], "running")
        self.assertEqual(self._wait_terminal(manager), "done")
        self.assertEqual([self.store.get_goal(g["id"])["state"] for g in self.goals],
                         ["done", "done"])

    def test_resume_rejects_running_project(self):
        manager = RunManager(self.store, autostart=False)
        self.store.update_project(self.project["id"],
                                  run={"state": "running", "active_goal_id": None})
        with self.assertRaisesRegex(ValueError, "not resumable"):
            manager.resume_project(self.project["id"])

    def test_parallel_contract_is_rejected(self):
        project = self.store.get_project(self.project["id"])
        project["contract"]["execution_mode"] = "parallel"
        self.store.update_project(project["id"], contract=project["contract"])
        manager = RunManager(self.store, autostart=False)
        with self.assertRaisesRegex(ValueError, "worktrees"):
            manager.start_project(project["id"])


if __name__ == "__main__":
    unittest.main()
