"""Parley management API (GUI nấc B). Requires: pip install -r requirements-web.txt.

Run: PARLEY_TOKEN=<secret> uvicorn parley.web.app:app --host 127.0.0.1 --port 8800
SECURITY (R1/R2): bind 127.0.0.1; bearer token MỌI route dữ liệu (header hoặc ?token= cho SSE);
ra ngoài CHỈ qua tunnel có danh tính. Sửa config/project_dir là bề mặt R1 — không trỏ production.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..store import Store
from ..manager import RunManager
from ..tasks import project_tasks
from ..config import Config
from ..backends import CommandProfile, GenericCliBackend
from ..advisorchat import parse_jsonl, start_prompt, turn_prompt
from ..planner import parse_plan
from .. import notify


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env at repo root into os.environ (no override
    of vars already set in the real environment). Zero-dependency, fail-soft.
    Lets `run.bat` / a checked-out .env supply PARLEY_* without typing them."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

TOKEN = os.environ.get("PARLEY_TOKEN", "")
_store = Store()
_mgr = RunManager(_store)
_backend = GenericCliBackend()

# Telegram forum notifier (opt-in via PARLEY_TG_TOKEN + PARLEY_TG_GROUP_ID). Fail-soft.
# Created at import so routes can read status; thread lifecycle bound to app lifespan.
_notifier = notify.from_env(_store, _mgr)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if _notifier is not None:
        _notifier.start()
    try:
        yield
    finally:
        if _notifier is not None:
            _notifier.stop()
            _notifier.join(timeout=5)


app = FastAPI(title="Parley", lifespan=_lifespan)


def _planner_runner(project_dir: str):
    cfg = Config(project_dir=project_dir)        # defaults: advisor_cmd (codex exec read-only)
    prof = CommandProfile(cfg.advisor_cmd, "planner")

    def runner(prompt: str) -> str:
        return _backend.run_once(prof, prompt, project_dir,
                                 cfg.limits.idle_timeout_s, cfg.limits.hard_timeout_s).stdout
    return runner


def _chat_run(p: dict, message: str) -> dict:
    """Warm advisor turn: start (lần đầu) hoặc resume; parse JSONL; lưu khi turn.completed."""
    cfg = Config(project_dir=p["project_dir"])
    sid = p.get("advisor_session")
    if sid:
        cmd = [a.replace("{session_id}", sid) for a in cfg.advisor_chat.resume_cmd]
        prompt = turn_prompt(message)
    else:
        cmd = list(cfg.advisor_chat.start_cmd)
        prompt = start_prompt(message)
    res = _backend.run_once(CommandProfile(cmd, "advisor"), prompt, p["project_dir"],
                            cfg.limits.idle_timeout_s, cfg.limits.hard_timeout_s)
    parsed = parse_jsonl(res.stdout)
    if parsed["completed"]:
        if not sid and parsed.get("thread_id"):
            _store.update_project(p["id"], advisor_session=parsed["thread_id"])
        _store.add_chat(p["id"], "user", message)
        _store.add_chat(p["id"], "advisor", parsed["reply"])
    return parsed


def _auth(authorization: str = "", token: str = "") -> None:
    supplied = token or (authorization[7:] if authorization.startswith("Bearer ") else "")
    if not TOKEN or supplied != TOKEN:
        raise HTTPException(401, "missing/invalid bearer token")


def _goal_or_404(gid: str) -> dict:
    g = _store.get_goal(gid)
    if not g:
        raise HTTPException(404, "unknown goal")
    return g


@app.get("/")
def index():
    return HTMLResponse((Path(__file__).parent / "index.html").read_text(encoding="utf-8"))


# ---- projects ----
@app.get("/projects")
def list_projects(authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    return _store.list_projects()


@app.get("/fs/list")
def fs_list(path: str = "", authorization: str = Header(default=""), token: str = ""):
    """Duyệt thư mục server để chọn project folder (localhost + token). Chỉ trả thư mục con.
    path='DRIVES' -> danh sách ổ đĩa (Windows); up từ gốc ổ -> 'DRIVES'."""
    _auth(authorization, token)
    if path == "DRIVES":
        try:
            drives = os.listdrives()        # Python 3.12+
        except Exception:
            drives = []
        return {"path": "DRIVES", "parent": None,
                "dirs": [{"name": d, "path": d} for d in drives]}
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
    except Exception:
        p = Path.home().resolve()
    if not p.is_dir():
        p = Path.home().resolve()
    dirs = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if e.is_dir() and not e.name.startswith("."):
                dirs.append({"name": e.name, "path": str(e)})
    except Exception:
        pass
    parent = "DRIVES" if p.parent == p else str(p.parent)
    return {"path": str(p), "parent": parent, "dirs": dirs}


@app.post("/projects")
async def add_project(request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    b = await request.json()
    if not b.get("name") or not b.get("project_dir"):
        raise HTTPException(400, "name and project_dir required")
    return _store.add_project(b["name"], b["project_dir"])


@app.patch("/projects/{pid}")
async def update_project(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    b = await request.json()
    p = _store.update_project(pid, name=b.get("name"), project_dir=b.get("project_dir"))
    if not p:
        raise HTTPException(404, "unknown project")
    return p


@app.put("/projects/{pid}/notify")
async def set_notify(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    """Bật/tắt + đặt level push Telegram cho project. body: {enabled?:bool, level?:milestone|escalate|verbose}."""
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    b = await request.json()
    notify_cfg = dict((_store.get_project(pid).get("notify") or {}))   # merge với hiện có
    if b.get("enabled") is not None:
        notify_cfg["enabled"] = bool(b["enabled"])
    if b.get("level") is not None:
        if b["level"] not in ("verbose", "milestone", "escalate"):
            raise HTTPException(400, "level must be verbose|milestone|escalate")
        notify_cfg["level"] = b["level"]
    p = _store.update_project(pid, notify=notify_cfg or None)
    return p.get("notify") or {}


@app.get("/notify/status")
def notify_status(authorization: str = Header(default=""), token: str = ""):
    """Trạng thái kết nối Telegram (không lộ token/group_id). configured=False nếu env chưa đặt."""
    _auth(authorization, token)
    if _notifier is None:
        return {"configured": False, "connected": False}
    return _notifier.status()


@app.post("/projects/{pid}/notify/test")
def notify_test(pid: str, authorization: str = Header(default=""), token: str = ""):
    """Gửi 1 message test vào topic của project (tạo topic nếu chưa có)."""
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    if _notifier is None:
        raise HTTPException(400, "notifier not configured (set PARLEY_TG_TOKEN/PARLEY_TG_GROUP_ID)")
    return _notifier.test_push(pid)


@app.get("/projects/{pid}")
def get_project(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    p = _store.get_project(pid)
    if not p:
        raise HTTPException(404, "unknown project")
    return p


@app.post("/projects/{pid}/init")
def init_project(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    return _store.init_project(pid)


@app.post("/projects/{pid}/plan")
async def plan_project(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    p = _store.get_project(pid)
    if not p:
        raise HTTPException(404, "unknown project")
    ideas = (await request.json()).get("ideas") or []
    return _store.plan_project(pid, ideas, _planner_runner(p["project_dir"]))


@app.put("/projects/{pid}/contract")
async def put_contract(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    try:
        return _store.set_contract_draft(pid, await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/contract/from_text")
async def contract_from_text(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    body = await request.json()
    contract = parse_plan(body.get("text", ""))
    if not contract.get("goals"):
        raise HTTPException(400, "no goals found in text")
    return _store.set_contract_draft(pid, contract)


@app.post("/projects/{pid}/approve")
def approve_contract(pid: str, strategy: str = "replace",
                     authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    try:
        return _store.approve_contract(pid, strategy=strategy)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/projects/{pid}/run")
def project_run_status(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    try:
        return _mgr.project_status(pid)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/projects/{pid}/run")
def run_project(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    try:
        return _mgr.start_project(pid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/stop")
def stop_project(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    try:
        return _mgr.stop_project(pid)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/projects/{pid}/resume")
def resume_project(pid: str, authorization: str = Header(default=""), token: str = ""):
    """Resume a blocked/stopped project: re-queue the blocking goal + restart worker."""
    _auth(authorization, token)
    try:
        return _mgr.resume_project(pid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/projects/{pid}/chat")
def get_chat(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    return _store.get_chat(pid)


@app.post("/projects/{pid}/chat")
async def post_chat(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    p = _store.get_project(pid)
    if not p:
        raise HTTPException(404, "unknown project")
    msg = (await request.json()).get("message", "").strip()
    if not msg:
        raise HTTPException(400, "message required")
    parsed = _chat_run(p, msg)
    if not parsed["completed"]:
        return {"completed": False, "error": "no turn.completed (timeout/error)"}
    return {"completed": True, "reply": parsed["reply"], "thread_id": parsed.get("thread_id")}


@app.post("/projects/{pid}/artifacts")
async def save_artifact(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    if not _store.get_project(pid):
        raise HTTPException(404, "unknown project")
    body = await request.json()
    try:
        return _store.save_artifact(pid, body.get("path", ""), body.get("content", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/projects/{pid}/propose_plan")
def propose_plan(pid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    p = _store.get_project(pid)
    if not p:
        raise HTTPException(404, "unknown project")
    msg = ('Hay CHOT ke hoach cho project. Tra ve DUY NHAT JSON: '
           '{"execution_mode":"sequential|parallel","goals":[{"title":"..."}],"reason":"..."}')
    parsed = _chat_run(p, msg)
    if not parsed["completed"]:
        return {"approved": False, "goals": [], "reason": "timeout/error", "execution_mode": "sequential"}
    contract = parse_plan(parsed["reply"])
    return _store.set_contract_draft(pid, contract)


# ---- goals ----
@app.get("/projects/{pid}/goals")
def list_goals(pid: str, include_deleted: bool = False,
               authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    return [_mgr.refresh(g["id"]) for g in _store.list_goals(pid)
            if include_deleted or g.get("state") != "deleted"]


@app.post("/projects/{pid}/goals")
async def add_goal(pid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    b = await request.json()
    if not b.get("goal"):
        raise HTTPException(400, "goal required")
    try:
        return _store.add_goal(pid, b["goal"])
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/goals/{gid}")
def get_goal(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    return _mgr.refresh(gid)


@app.patch("/goals/{gid}")
async def edit_goal(gid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    g = _goal_or_404(gid)
    if g.get("state") in ("running", "stopping"):
        raise HTTPException(409, "cannot edit a running goal; stop it first")
    b = await request.json()
    return _store.update_goal(gid, goal=b.get("goal"), description=b.get("description"))


@app.delete("/goals/{gid}")
def delete_goal(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    try:
        return _store.delete_goal(gid)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/goals/{gid}/run")
def run_goal(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    return _mgr.start(gid)


@app.post("/goals/{gid}/control")
async def control_goal(gid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    b = await request.json()
    verdict = b.get("verdict", "continue")
    if verdict == "stop":
        return _mgr.stop(gid)
    return _mgr.control(gid, verdict, b.get("inject", ""))


@app.get("/goals/{gid}/status")
def goal_status(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    g = _goal_or_404(gid)
    f = Path(g["data_dir"]) / "status.json"
    st = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return {"goal": _mgr.refresh(gid), "status": st}


@app.get("/goals/{gid}/tasks")
def goal_tasks(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    g = _goal_or_404(gid)
    conv = Path(g["data_dir"]) / "conversation.ndjson"
    events = []
    if conv.exists():
        for line in conv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return project_tasks(events)


@app.get("/goals/{gid}/live")
def goal_live(gid: str, authorization: str = Header(default=""), token: str = ""):
    """LS-019: tail the streaming live-logs of this goal's attempts. Returns each
    attempt's pid header + last non-empty line + mtime so a watcher can tell
    'working' from 'hung' while a CLI runs silently (the attempt .log only fills
    after the process ends — useless for a 0-byte timeout)."""
    _auth(authorization, token)
    g = _goal_or_404(gid)
    d = Path(g["data_dir"]) / "live"
    out = []
    if d.exists():
        for f in sorted(d.glob("*.log")):
            try:
                lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
            except Exception:
                continue
            header = next((l for l in lines if l.startswith("# pid=")), "")
            last = next((l for l in reversed(lines) if not l.startswith("#")), "")
            out.append({"attempt": f.stem, "pid_header": header,
                        "last_line": last[:300], "mtime": f.stat().st_mtime,
                        "running": not any(l.startswith("# end") for l in lines)})
    return {"attempts": out}


@app.get("/goals/{gid}/config")
def get_config(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    return _store.read_config(gid) or {}


@app.put("/goals/{gid}/config")
async def put_config(gid: str, request: Request, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    _goal_or_404(gid)
    cfg = await request.json()
    if not _store.write_config(gid, cfg):
        raise HTTPException(400, "write failed")
    return {"ok": True}


@app.get("/goals/{gid}/feed")
def goal_feed(gid: str, authorization: str = Header(default=""), token: str = ""):
    _auth(authorization, token)
    g = _goal_or_404(gid)
    conv = Path(g["data_dir"]) / "conversation.ndjson"

    def gen():
        pos = 0
        while True:
            if conv.exists():
                lines = conv.read_text(encoding="utf-8").splitlines()
                for line in lines[pos:]:
                    if line.strip():
                        yield f"data: {line}\n\n"
                pos = len(lines)
            time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")
