import json
import unittest
from pathlib import Path

from _util import temporary_directory
from parley.store import Store
from parley.manager import RunManager
from parley import notify


GROUP_ID = "-1001234567890"
ALLOWED_UID = "555"


class FakeForum:
    """Fake transport-level forum: ghi lại mọi call, trả kết quả lập trình được."""
    def __init__(self, group_id=GROUP_ID):
        self.group_id = str(group_id)
        self.sent = []          # (thread_id, text)
        self.topics = {}        # name -> thread_id
        self._next_tid = 100
        self._updates = []      # getUpdates trả ra

    # API mirror TelegramForum
    def create_topic(self, name):
        self._next_tid += 1
        self.topics[name] = self._next_tid
        return self._next_tid

    def send(self, text, thread_id=None):
        self.sent.append((thread_id, text))
        return {"ok": True}

    def get_updates(self, offset, timeout=30):
        out = [u for u in self._updates if u["update_id"] >= offset]
        self._updates = []
        return out

    def queue_message(self, update_id, thread_id, text, chat_id=None,
                      from_id=ALLOWED_UID, is_topic=True):
        """Mặc định: gửi từ group đúng, user trong allowlist, trong topic -> hợp lệ."""
        self._updates.append({"update_id": update_id, "message": {
            "message_thread_id": thread_id, "text": text,
            "chat": {"id": self.group_id if chat_id is None else chat_id},
            "from": {"id": from_id},
            "is_topic_message": is_topic,
        }})


class TestEventLevel(unittest.TestCase):
    def test_static_levels(self):
        self.assertEqual(notify._event_level({"type": "slice_done"}), "milestone")
        self.assertEqual(notify._event_level({"type": "stopped"}), "escalate")
        self.assertEqual(notify._event_level({"type": "dispatch"}), "verbose")

    def test_content_dependent_levels(self):
        self.assertEqual(notify._event_level({"type": "advisor", "verdict": "APPROVE"}), "milestone")
        self.assertEqual(notify._event_level({"type": "advisor", "verdict": "continue"}), "verbose")
        self.assertEqual(notify._event_level({"type": "gov", "verdict": "stop"}), "milestone")
        self.assertEqual(notify._event_level({"type": "gov", "verdict": "continue"}), "verbose")
        self.assertEqual(notify._event_level({"type": "verify", "exit": 1}), "milestone")
        self.assertEqual(notify._event_level({"type": "verify", "exit": 0}), "verbose")

    def test_allowed_ordering(self):
        self.assertTrue(notify._allowed("escalate", "milestone"))
        self.assertTrue(notify._allowed("milestone", "milestone"))
        self.assertFalse(notify._allowed("verbose", "milestone"))


class TestRender(unittest.TestCase):
    def test_icons_and_escape(self):
        self.assertIn("✅", notify.render_event({"type": "advisor", "verdict": "APPROVE", "slice": "s1"}, "P"))
        self.assertIn("❌", notify.render_event({"type": "advisor", "verdict": "REJECT", "slice": "s1"}, "P"))
        self.assertIn("🔴", notify.render_event({"type": "verify", "exit": 2, "failed_gate": "cargo"}, "P"))
        self.assertIn("⛔", notify.render_event({"type": "stopped", "reason": "observer"}, "P"))
        self.assertIn("📦", notify.render_event({"type": "slice_done", "slice": "s", "commit": "abc1234567"}, "P"))
        # HTML escape của project name
        self.assertIn("&lt;x&gt;", notify.render_event({"type": "phase_start", "phase": 1}, "<x>"))


class TestNotifierWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        (self.proj / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self.store = Store(home=str(self.home))
        self.mgr = RunManager(self.store, autostart=False)
        self.forum = FakeForum()
        self.worker = notify.NotifierWorker(self.store, self.mgr, self.forum,
                                            default_level="milestone",
                                            cursor_path=str(self.home / "cursor.json"),
                                            allowed_user_ids=[ALLOWED_UID])
        # project + contract approved -> goal_ids
        self.p = self.store.add_project("Demo", str(self.proj))
        # Push is opt-in per project: enable it so push tests exercise the path.
        self.store.update_project(self.p["id"], notify={"enabled": True, "level": "milestone"})
        self.store.set_contract_draft(self.p["id"], {
            "execution_mode": "sequential",
            "goals": [{"title": "one"}, {"title": "two"}], "reason": "r",
        })
        self.goals = self.store.approve_contract(self.p["id"])

    def _emit(self, gid, **ev):
        g = self.store.get_goal(gid)
        conv = Path(g["data_dir"]) / "conversation.ndjson"
        conv.parent.mkdir(parents=True, exist_ok=True)
        prev = 0
        if conv.exists():
            for ln in conv.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    prev = max(prev, json.loads(ln).get("id", 0))
        ev.setdefault("id", prev + 1)
        with conv.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ev) + "\n")

    def test_pushes_milestone_skips_verbose(self):
        gid = self.goals[0]["id"]
        self._emit(gid, type="dispatch", role="coder", slice="s1")        # verbose -> skip
        self._emit(gid, type="advisor", verdict="APPROVE", slice="s1")    # milestone -> push
        self.worker._tick_push()
        texts = [t for _, t in self.forum.sent]
        self.assertTrue(any("APPROVE" in t for t in texts))
        self.assertFalse(any("Giao" in t for t in texts))   # dispatch không push

    def test_topic_created_once_and_cached(self):
        gid = self.goals[0]["id"]
        self._emit(gid, type="phase_start", phase=1)
        self.worker._tick_push()
        self.assertEqual(self.store.get_project(self.p["id"]).get("tg_thread_id"),
                         self.forum.topics["Demo"])
        # tick lần 2: không tạo topic mới
        before = self.forum._next_tid
        self.worker._tick_push()
        self.assertEqual(self.forum._next_tid, before)

    def test_cursor_dedupe_no_resend(self):
        gid = self.goals[0]["id"]
        self._emit(gid, type="phase_start", phase=1)
        self.worker._tick_push()
        n1 = len(self.forum.sent)
        self.worker._tick_push()   # không có event mới
        self.assertEqual(len(self.forum.sent), n1)

    def test_project_progress_transition(self):
        # goal[0] done -> "1/2 task xong"
        self.store.update_goal(self.goals[0]["id"], state="done")
        self.worker._tick_push()
        self.assertTrue(any("1/2" in t for _, t in self.forum.sent))

    def test_topic_failure_does_not_fall_back_or_advance_cursor(self):
        gid = self.goals[0]["id"]
        self._emit(gid, type="phase_start", phase=1)
        self.forum.create_topic = lambda name: None
        self.worker._tick_push()
        self.assertEqual(self.forum.sent, [])
        self.assertEqual(self.worker._state["goals"].get(gid, 0), 0)

    def test_failed_transition_send_retries_snapshot(self):
        self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        self.store.update_goal(self.goals[0]["id"], state="done")
        self.forum.send = lambda text, thread_id=None: None
        self.worker._tick_push()
        snap = self.worker._state["proj"][self.p["id"]]
        self.assertEqual(snap.get("done", 0), 0)

    def test_off_disables_push(self):
        self.store.update_project(self.p["id"], notify={"enabled": False})
        gid = self.goals[0]["id"]
        self._emit(gid, type="phase_start", phase=1)
        self.worker._tick_push()
        self.assertEqual(self.forum.sent, [])

    def test_reply_stop_writes_control(self):
        # set topic + active goal
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        run = {"state": "running", "active_goal_id": gid}
        self.store.update_project(self.p["id"], run=run)
        self.forum.queue_message(1, tid, "/stop")
        self.worker._tick_commands()
        ctl = json.loads((Path(self.store.get_goal(gid)["data_dir"]) / "control.json").read_text(encoding="utf-8"))
        self.assertEqual(ctl["verdict"], "stop")

    def test_reply_steer_writes_inject(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        self.store.update_project(self.p["id"], run={"state": "running", "active_goal_id": gid})
        self.forum.queue_message(5, tid, "/steer ưu tiên test")
        self.worker._tick_commands()
        ctl = json.loads((Path(self.store.get_goal(gid)["data_dir"]) / "control.json").read_text(encoding="utf-8"))
        self.assertEqual((ctl["verdict"], ctl["inject"]), ("steer", "ưu tiên test"))

    def test_unknown_command_ignored(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        self.store.update_project(self.p["id"], run={"state": "running",
                                                     "active_goal_id": self.goals[0]["id"]})
        self.forum.queue_message(9, tid, "hello world")
        self.worker._tick_commands()
        # không ghi control.json
        self.assertFalse((Path(self.store.get_goal(self.goals[0]["id"])["data_dir"]) / "control.json").exists())

    # ---- security: command authorization ----
    def _control_path(self, gid):
        return Path(self.store.get_goal(gid)["data_dir"]) / "control.json"

    def test_command_rejected_from_unallowed_user(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        self.store.update_project(self.p["id"], run={"state": "running", "active_goal_id": gid})
        self.forum.queue_message(1, tid, "/stop", from_id="999")   # not in allowlist
        self.worker._tick_commands()
        self.assertFalse(self._control_path(gid).exists())
        # người dùng được cảnh báo từ chối quyền
        self.assertTrue(any("không có quyền" in t for _, t in self.forum.sent))

    def test_command_rejected_from_wrong_chat(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        self.store.update_project(self.p["id"], run={"state": "running", "active_goal_id": gid})
        self.forum.queue_message(1, tid, "/stop", chat_id="-100999")  # foreign chat
        self.worker._tick_commands()
        self.assertFalse(self._control_path(gid).exists())

    def test_command_rejected_outside_topic(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        self.store.update_project(self.p["id"], run={"state": "running", "active_goal_id": gid})
        self.forum.queue_message(1, tid, "/stop", is_topic=False)   # General/DM
        self.worker._tick_commands()
        self.assertFalse(self._control_path(gid).exists())

    def test_continue_on_blocked_calls_resume(self):
        tid = self.worker._ensure_topic(self.store.get_project(self.p["id"]))
        gid = self.goals[0]["id"]
        calls = []
        self.mgr.resume_project = lambda pid: calls.append(pid)
        self.store.update_project(self.p["id"], run={"state": "blocked", "active_goal_id": gid})
        self.forum.queue_message(1, tid, "/continue")
        self.worker._tick_commands()
        self.assertEqual(calls, [self.p["id"]])


class TestNotifierIntrospection(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.store = Store(home=str(self.home))
        self.mgr = RunManager(self.store, autostart=False)
        self.forum = FakeForum()
        self.forum.get_me = lambda: {"ok": True, "result": {"username": "parley_bot"}}
        self.worker = notify.NotifierWorker(self.store, self.mgr, self.forum,
                                            default_level="milestone",
                                            cursor_path=str(self.home / "cursor.json"),
                                            allowed_user_ids=["42"])

    def test_status_reports_connection_no_secrets(self):
        st = self.worker.status()
        self.assertEqual(st["connected"], True)
        self.assertEqual(st["bot_username"], "parley_bot")
        self.assertTrue(st["command_control"])     # allowlist non-empty
        # không lộ token/group_id
        self.assertNotIn("token", st)
        self.assertNotIn("group_id", st)

    def test_test_push_creates_topic_and_sends(self):
        p = self.store.add_project("Solo", str(self.home))
        res = self.worker.test_push(p["id"])
        self.assertTrue(res["ok"])
        self.assertTrue(any("Test push" in t for _, t in self.forum.sent))

    def test_status_accepts_real_telegram_get_me_shape(self):
        def transport(method, params):
            self.assertEqual(method, "getMe")
            return {"ok": True, "result": {"id": 7, "username": "real_bot"}}

        forum = notify.TelegramForum("secret", GROUP_ID, transport=transport)
        worker = notify.NotifierWorker(self.store, self.mgr, forum,
                                       cursor_path=str(self.home / "real-cursor.json"))
        st = worker.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["bot_username"], "real_bot")

    def test_transport_refuses_general_topic(self):
        calls = []
        forum = notify.TelegramForum(
            "secret", GROUP_ID,
            transport=lambda method, params: calls.append((method, params)) or {"ok": True},
        )
        self.assertIsNone(forum.send("must stay in a project topic", None))
        self.assertEqual(calls, [])

    def test_worker_stop_can_join_cleanly(self):
        worker = notify.NotifierWorker(self.store, self.mgr, self.forum, poll_interval=0.01,
                                       cursor_path=str(self.home / "join-cursor.json"))
        worker.start()
        worker.stop()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())


class TestResumeProject(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()
        (self.proj / "Cargo.toml").write_text("[package]", encoding="utf-8")
        self.store = Store(home=str(self.home))
        self.mgr = RunManager(self.store, autostart=False)
        self.p = self.store.add_project("Demo", str(self.proj))
        self.store.set_contract_draft(self.p["id"], {
            "execution_mode": "sequential",
            "goals": [{"title": "one"}, {"title": "two"}], "reason": "r",
        })
        self.goals = self.store.approve_contract(self.p["id"])

    def test_resume_requeues_blocked_goal_and_clears_control(self):
        gid = self.goals[0]["id"]
        # simulate a blocked project at goal 0 (stopped) with a stale control.json
        self.store.update_goal(gid, state="stopped", stop_reason="observer")
        ctl = Path(self.store.get_goal(gid)["data_dir"]) / "control.json"
        ctl.parent.mkdir(parents=True, exist_ok=True)
        ctl.write_text(json.dumps({"verdict": "stop"}), encoding="utf-8")
        self.store.update_project(self.p["id"], run={"state": "blocked", "active_goal_id": gid})
        # avoid spawning a real subprocess when tick_project picks up the requeued goal
        self.mgr.start = lambda g, command=None: self.store.update_goal(g, state="running")
        self.mgr._ensure_project_worker = lambda pid: None
        self.mgr.resume_project(self.p["id"])
        self.assertFalse(ctl.exists())                       # stale control cleared
        self.assertEqual(self.store.get_goal(gid)["stop_reason"], "")

    def test_resume_rejects_running_project(self):
        self.store.update_project(self.p["id"], run={"state": "running", "active_goal_id": None})
        with self.assertRaises(ValueError):
            self.mgr.resume_project(self.p["id"])


if __name__ == "__main__":
    unittest.main()
