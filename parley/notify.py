"""Parley → Telegram forum notifications (push per-project + 2-way control).

Opt-in via env (giống PARLEY_TOKEN):
  PARLEY_TG_TOKEN     bot token
  PARLEY_TG_GROUP_ID  supergroup id (bật Topics); bot phải là admin có quyền Manage Topics

Thiết kế (xem plan): mỗi project = 1 forum topic. Notifier là 1 daemon thread đọc
conversation.ndjson của goal đang active + so snapshot project-state, push event theo level
(milestone|escalate mặc định), và long-poll getUpdates để nhận lệnh reply -> control.json.

KHÔNG thêm dependency: chỉ dùng urllib.request (stdlib). Mọi I/O mạng bọc try/except,
lỗi mạng KHÔNG được làm chết worker hay ảnh hưởng harness/manager.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from html import escape
from pathlib import Path

# ---- event -> level ----------------------------------------------------------
# escalate: cần người để mắt ngay. milestone: cột mốc tiến độ. verbose: traffic worker.
EVENT_LEVEL = {
    "user_goal": "milestone",
    "phase_start": "milestone",
    "phase_end": "milestone",
    "slice_done": "milestone",
    "push": "milestone",
    "housekeeping": "verbose",
    "dispatch": "verbose",
    "report": "verbose",
    "resume": "escalate",
    "paused": "escalate",
    "turn_error": "escalate",
    "stopped": "escalate",
    # ADR-15 executor fallback events
    "executor_retry": "verbose",
    "executor_fallback": "milestone",
    "executor_error": "escalate",
    "executor_exhausted": "escalate",
    # advisor/gov/verify: level phụ thuộc nội dung -> xử lý trong _event_level()
}

_LEVELS = ("verbose", "milestone", "escalate")


def _event_level(ev: dict) -> str:
    """Level của một event, tính cả các trường hợp phụ thuộc verdict/exit."""
    t = ev.get("type")
    if t == "advisor":
        return "milestone" if ev.get("verdict") in ("APPROVE", "REJECT") else "verbose"
    if t == "gov":
        return "milestone" if ev.get("verdict") in ("steer", "stop") else "verbose"
    if t == "verify":
        return "milestone" if ev.get("exit") not in (0, None) else "verbose"
    return EVENT_LEVEL.get(t, "verbose")


def _allowed(level: str, min_level: str) -> bool:
    """level >= min_level theo thứ tự verbose < milestone < escalate."""
    return _LEVELS.index(level) >= _LEVELS.index(min_level)


# ---- render ------------------------------------------------------------------
def render_event(ev: dict, project_name: str) -> str:
    """Render 1 event thành HTML message, mirror icon/label của web UI evView()."""
    t = ev.get("type")
    head = f"<b>{escape(project_name)}</b>"

    def line(icon, body):
        return f"{head}\n{icon} {body}"

    if t == "user_goal":
        return line("🎯", f"Goal bắt đầu: {escape(str(ev.get('goal', ''))[:200])}")
    if t == "phase_start":
        return line("▶️", f"Phase {ev.get('phase')} bắt đầu")
    if t == "phase_end":
        return line("⏹️", f"Phase {ev.get('phase')} kết thúc")
    if t == "advisor":
        v = ev.get("verdict")
        icon = "✅" if v == "APPROVE" else "❌" if v == "REJECT" else "💬"
        return line(icon, f"Advisor <b>{escape(str(v))}</b> · slice <code>{escape(str(ev.get('slice', '')))}</code>")
    if t == "gov":
        v = ev.get("verdict")
        icon = "🛑" if v == "stop" else "↪️" if v == "steer" else "⚖️"
        return line(icon, f"Supervisor <b>{escape(str(v))}</b>: {escape(str(ev.get('reason', ''))[:200])}")
    if t == "dispatch":
        return line("📤", f"Giao <b>{escape(str(ev.get('role', '')))}</b> → <code>{escape(str(ev.get('slice', '')))}</code>")
    if t == "report":
        return line("📥", f"<b>{escape(str(ev.get('role', '')))}</b> báo cáo · done={ev.get('done')} · verdict={escape(str(ev.get('verdict')))}")
    if t == "verify":
        if ev.get("exit") not in (0, None):
            return line("🔴", f"Verify FAIL gate=<code>{escape(str(ev.get('failed_gate', '')))}</code> (slice {escape(str(ev.get('slice', '')))})")
        return line("🟢", f"Verify pass · slice {escape(str(ev.get('slice', '')))}")
    if t == "slice_done":
        sha = str(ev.get("commit") or "")[:8]
        return line("📦", f"Slice <code>{escape(str(ev.get('slice', '')))}</code> xong · commit <code>{escape(sha)}</code>")
    if t == "push":
        return line("🚀" if ev.get("ok") else "⚠️", f"Push <code>{escape(str(ev.get('branch', '')))}</code> · {'OK' if ev.get('ok') else 'FAIL'}")
    if t == "resume":
        return line("⚠️", f"Chờ người — {escape(str(ev.get('note', 'resume')))}")
    if t == "paused":
        return line("⏸️", f"Tạm dừng — {escape(str(ev.get('note', 'paused')))}")
    if t == "turn_error":
        return line("⚠️", f"Lỗi lượt: {escape(str(ev.get('reason', ''))[:200])}")
    if t == "stopped":
        return line("⛔", f"Dừng: {escape(str(ev.get('reason', '')))}")
    if t == "housekeeping":
        return line("📝", f"Cập nhật {escape(str(ev.get('doc', '')))}")
    if t in ("executor_fallback", "executor_retry"):
        frm = f"{ev.get('from_provider', '')}/{ev.get('from_model', '') or '-'}"
        to = f"{ev.get('to_provider', '')}/{ev.get('to_model', '') or '-'}"
        if t == "executor_retry":
            return line("🔁", f"Retry <code>{escape(str(ev.get('provider', '')))}/"
                              f"{escape(str(ev.get('model', '') or '-'))}</code> "
                              f"(lần {ev.get('attempt')}) · {escape(str(ev.get('reason', '')))}")
        return line("🔀", f"Fallback <code>{escape(frm)}</code> → <code>{escape(to)}</code> · "
                          f"{escape(str(ev.get('reason', '')))}")
    if t in ("executor_error", "executor_exhausted"):
        role = escape(str(ev.get("role", "")))
        return line("🆘", f"Executor STUCK ({role}): {escape(str(ev.get('reason', '')))} — "
                          f"cần người can thiệp")
    # fallback
    return line("⚙️", escape(t or "event"))


# ---- Telegram transport ------------------------------------------------------
class TelegramForum:
    """Bot API client tối thiểu: sendMessage / createForumTopic / getUpdates.

    transport(method, params)->dict cho phép inject fake trong test (không gọi mạng).
    """

    def __init__(self, token: str, group_id: str, transport=None):
        self.token = token
        self.group_id = str(group_id)
        self._transport = transport or self._http

    def _http(self, method: str, params: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode("utf-8"))

    def call(self, method: str, **params) -> dict | None:
        try:
            return self._transport(method, params)
        except Exception:
            return None

    def create_topic(self, name: str) -> int | None:
        res = self.call("createForumTopic", chat_id=self.group_id, name=name[:128])
        if res and res.get("ok"):
            return res["result"].get("message_thread_id")
        return None

    # Telegram hard-limits a message to 4096 chars. Cut well below it so an
    # unclosed HTML tag from truncation can't break parse_mode=HTML rendering.
    _MAX_TEXT = 3900

    def send(self, text: str, thread_id: int | None = None) -> dict | None:
        if thread_id is None:
            return None
        if len(text) > self._MAX_TEXT:
            text = text[:self._MAX_TEXT] + "\n… (cắt bớt)"
        params = {"chat_id": self.group_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        return self.call("sendMessage", **params)

    def get_updates(self, offset: int, timeout: int = 30) -> list:
        res = self.call("getUpdates", offset=offset, timeout=timeout,
                        allowed_updates=json.dumps(["message"]))
        if res and res.get("ok"):
            return res.get("result") or []
        return []

    def get_me(self) -> dict | None:
        """Verify the bot connection without exposing the token. Returns the bot
        user dict (id, username, …) on success, None on any failure."""
        res = self.call("getMe")
        if res and res.get("ok"):
            return res.get("result") or {}
        return None


# ---- worker ------------------------------------------------------------------
class NotifierWorker(threading.Thread):
    """Daemon thread: push events + project transitions, poll reply -> control.json.

    Pattern giống manager._project_worker. Khởi động từ web/app.py khi env đủ.
    """

    COMMANDS = {"/stop", "/pause", "/continue", "/steer"}

    def __init__(self, store, manager, forum: TelegramForum, poll_interval: float = 2.0,
                 default_level: str = "milestone", cursor_path=None, allowed_user_ids=None):
        super().__init__(daemon=True)
        self.store = store
        self.mgr = manager
        self.forum = forum
        self.poll_interval = poll_interval
        self.default_level = default_level
        self.cursor_path = Path(cursor_path) if cursor_path else (store.home / "notify_cursor.json")
        # Allowlist of Telegram user ids permitted to issue control commands.
        # Empty set = no one is authorized (commands ignored) — fail closed.
        self.allowed_user_ids = {str(u) for u in (allowed_user_ids or [])}
        self._state = self._load_cursor()
        self._stop_event = threading.Event()
        # Cache getMe so rapid status() calls (many tabs, test + poll) don't hammer
        # Telegram. (timestamp, result-dict); TTL below.
        self._status_cache = (0.0, None)
        self._status_ttl = 20.0

    # ---- cursor persistence ----
    def _load_cursor(self) -> dict:
        if self.cursor_path.exists():
            try:
                return json.loads(self.cursor_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"goals": {}, "offset": 0, "proj": {}}

    def _save_cursor(self) -> None:
        try:
            tmp = self.cursor_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cursor_path)
        except Exception:
            pass

    # ---- helpers ----
    def _min_level(self, project: dict) -> str:
        # Opt-in per project: push only when notify.enabled is explicitly True.
        n = project.get("notify") or {}
        if n.get("enabled") is not True:
            return "off"
        lvl = n.get("level") or self.default_level
        return lvl if lvl in _LEVELS else self.default_level

    def _ensure_topic(self, project: dict) -> int | None:
        tid = project.get("tg_thread_id")
        if tid is not None:
            return tid
        tid = self.forum.create_topic(project["name"])
        if tid is not None:
            self.store.update_project(project["id"], tg_thread_id=tid)
        return tid

    def _read_events(self, gid: str, data_dir: str) -> list:
        conv = Path(data_dir) / "conversation.ndjson"
        if not conv.exists():
            return []
        out = []
        last = self._state["goals"].get(gid, 0)
        for line in conv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("id", 0) > last:
                out.append(ev)
        return out

    # ---- main loop ----
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick_push()
                self._tick_commands()
            except Exception:
                pass
            self._save_cursor()
            self._stop_event.wait(self.poll_interval)

    def stop(self) -> None:
        self._stop_event.set()

    def _tick_push(self) -> None:
        for project in self.store.list_projects():
            min_level = self._min_level(project)
            if min_level == "off":
                continue
            pid = project["id"]
            run = project.get("run") or {}
            gids = (project.get("contract") or {}).get("goal_ids") or []
            # 1) events của các goal trong contract (active + vừa terminal)
            last_emit = self._state.setdefault("proj", {}).setdefault(pid, {})
            for gid in gids:
                g = self.store.get_goal(gid)
                if not g:
                    continue
                events = self._read_events(gid, g["data_dir"])
                if not events:
                    continue
                tid = self._ensure_topic(project)
                for ev in events:
                    lvl = _event_level(ev)
                    if _allowed(lvl, min_level):
                        sig = (ev.get("type"), ev.get("slice"), ev.get("verdict"))
                        if sig != last_emit.get("last_sig"):
                            if tid is None:
                                # Never fall back to General. Keep the cursor
                                # unchanged so topic creation retries next tick.
                                break
                            res = self.forum.send(render_event(ev, project["name"]), tid)
                            if not res:
                                # Network send failed: stop here and keep the cursor
                                # at the last delivered event so we retry next tick.
                                break
                            last_emit["last_sig"] = list(sig)
                    self._state["goals"][gid] = ev.get("id", self._state["goals"].get(gid, 0))
            # 2) project transitions (derived: i/n done, blocked, done)
            self._push_project_transition(project, run, gids, min_level)

    def _push_project_transition(self, project, run, gids, min_level) -> None:
        pid = project["id"]
        snap = self._state["proj"].setdefault(pid, {})
        done_now = sum(1 for gid in gids
                       if (self.store.get_goal(gid) or {}).get("state") == "done")
        total = len(gids)
        prev_done = snap.get("done", 0)
        prev_state = snap.get("run_state")
        cur_state = run.get("state")
        tid = self._ensure_topic(project) if gids else None
        if gids and tid is None:
            # Never fall back to General. Preserve the snapshot so topic
            # creation and transition delivery retry on the next tick.
            return
        # goal mới done -> i/n
        if done_now > prev_done and total and _allowed("milestone", min_level):
            nxt = next((self.store.get_goal(gid) for gid in gids
                        if (self.store.get_goal(gid) or {}).get("state") in ("idle", "queued")), None)
            nxt_t = f" · next: {escape(str(nxt.get('goal', ''))[:60])}" if nxt else ""
            if not self.forum.send(
                    f"<b>{escape(project['name'])}</b>\n☑️ {done_now}/{total} task xong{nxt_t}", tid):
                return
        # blocked / done transitions
        if cur_state != prev_state:
            if cur_state == "blocked" and _allowed("escalate", min_level):
                gid = run.get("active_goal_id")
                g = self.store.get_goal(gid) if gid else None
                reason = run.get("stop_reason") or "blocked"
                if not self.forum.send(f"<b>{escape(project['name'])}</b>\n🚧 BLOCKED tại "
                                       f"{escape(str((g or {}).get('goal', '')))[:60]} ({escape(str(reason))})", tid):
                    return
            elif cur_state == "done" and _allowed("milestone", min_level):
                if not self.forum.send(
                        f"<b>{escape(project['name'])}</b>\n🎉 Project hoàn tất ({total}/{total})", tid):
                    return
        snap["done"] = done_now
        snap["run_state"] = cur_state

    # ---- 2-way control ----
    def _tick_commands(self) -> None:
        offset = self._state.get("offset", 0)
        updates = self.forum.get_updates(offset, timeout=0)
        for u in updates:
            self._state["offset"] = u.get("update_id", offset) + 1
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
            thread_id = msg.get("message_thread_id")
            if not text or thread_id is None:
                continue
            # Security: only accept commands from the configured group, from an
            # allowlisted user, posted inside a forum topic (not General/DM).
            if str((msg.get("chat") or {}).get("id")) != self.forum.group_id:
                continue
            if not msg.get("is_topic_message"):
                continue
            uid = str((msg.get("from") or {}).get("id"))
            if uid not in self.allowed_user_ids:
                # Only warn for actual command attempts, not arbitrary chatter.
                if text.split(maxsplit=1)[0].lower() in self.COMMANDS:
                    self.forum.send("⛔ Bạn không có quyền điều khiển harness.", thread_id)
                continue
            self._handle_command(thread_id, text)

    def _project_by_thread(self, thread_id: int) -> dict | None:
        for p in self.store.list_projects():
            if p.get("tg_thread_id") == thread_id:
                return p
        return None

    # ---- introspection for API (no secrets leaked) ----
    def status(self) -> dict:
        """Connection status for the UI. Never returns the token/group_id.
        getMe is cached for _status_ttl seconds to avoid hammering Telegram."""
        now = time.time()
        ts, cached = self._status_cache
        if cached is not None and (now - ts) < self._status_ttl:
            return cached
        me = self.forum.get_me() or {}
        # TelegramForum.get_me returns the bot user object. Accept the wrapped
        # fake-transport shape too for compatibility with injected test clients.
        bot = me.get("result") if me.get("ok") else me
        bot = bot if isinstance(bot, dict) else {}
        ok = bool(bot)
        result = {
            "configured": True,
            "connected": ok,
            "bot_username": bot.get("username") if ok else None,
            "default_level": self.default_level,
            "command_control": bool(self.allowed_user_ids),
            "running": self.is_alive(),
        }
        self._status_cache = (now, result)
        return result

    def test_push(self, pid: str) -> dict:
        """Send a one-off test message into the project's topic. Returns {ok,...}."""
        project = self.store.get_project(pid)
        if not project:
            return {"ok": False, "error": "unknown project"}
        tid = self._ensure_topic(project)
        if tid is None:
            return {"ok": False, "error": "topic unavailable"}
        res = self.forum.send(f"<b>{escape(project['name'])}</b>\n🔔 Test push từ Parley.", tid)
        return {"ok": bool(res), "thread_id": tid}

    def _handle_command(self, thread_id: int, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        if cmd not in self.COMMANDS:
            return
        project = self._project_by_thread(thread_id)
        if not project:
            return
        run = project.get("run") or {}
        gid = run.get("active_goal_id")
        # /continue on a blocked/stopped project must actually resume (re-queue +
        # restart worker); writing control.json alone reaches a dead process.
        if cmd == "/continue" and run.get("state") in ("blocked", "stopped"):
            try:
                self.mgr.resume_project(project["id"])
                self.forum.send("▶️ Đã resume project.", thread_id)
            except Exception as e:
                self.forum.send(f"⚠️ Lỗi resume: {escape(str(e))}", thread_id)
            return
        if not gid:
            self.forum.send("⚠️ Không có goal đang active để điều khiển.", thread_id)
            return
        inject = parts[1] if len(parts) > 1 else ""
        try:
            if cmd == "/stop":
                self.mgr.stop(gid)
                self.forum.send("⛔ Đã gửi lệnh stop.", thread_id)
            elif cmd == "/pause":
                self.mgr.control(gid, "pause")
                self.forum.send("⏸️ Đã gửi lệnh pause.", thread_id)
            elif cmd == "/continue":
                self.mgr.control(gid, "continue")
                self.forum.send("▶️ Đã gửi lệnh continue.", thread_id)
            elif cmd == "/steer":
                self.mgr.control(gid, "steer", inject)
                self.forum.send(f"↪️ Đã steer: {escape(inject[:200])}", thread_id)
        except Exception as e:
            self.forum.send(f"⚠️ Lỗi xử lý lệnh: {escape(str(e))}", thread_id)


def from_env(store, manager, env: dict | None = None):
    """Tạo NotifierWorker từ env nếu đủ token+group; None nếu không bật."""
    import os
    env = env if env is not None else os.environ
    token = env.get("PARLEY_TG_TOKEN", "")
    group = env.get("PARLEY_TG_GROUP_ID", "")
    if not token or not group:
        return None
    level = env.get("PARLEY_TG_LEVEL", "milestone")
    # Comma/space-separated Telegram user ids allowed to issue control commands.
    # Empty = fail closed (no command authority; push-only).
    raw_ids = env.get("PARLEY_TG_ALLOWED_USER_IDS", "")
    allowed = [tok for tok in raw_ids.replace(",", " ").split() if tok]
    forum = TelegramForum(token, group)
    return NotifierWorker(store, manager, forum, default_level=level,
                          allowed_user_ids=allowed)
