"""Run individual goals and approved sequential project contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


class RunManager:
    def __init__(self, store, command_factory=None, poll_interval: float = 0.5, autostart: bool = True):
        self.store = store
        self.command_factory = command_factory
        self.poll_interval = poll_interval
        self._procs = {}
        self._project_threads = {}
        self._lock = threading.RLock()
        if autostart:
            for project in self.store.list_projects():
                if (project.get("run") or {}).get("state") == "running":
                    self._ensure_project_worker(project["id"])

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _terminal_reason(goal: dict) -> str | None:
        conv = Path(goal["data_dir"]) / "conversation.ndjson"
        if not conv.exists():
            return None
        for line in reversed(conv.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") == "stopped":
                return event.get("reason") or "unknown"
        return None

    @staticmethod
    def _state_for_reason(reason: str | None) -> str:
        if reason == "done":
            return "done"
        if reason == "paused":
            return "paused"
        if reason in ("observer", "stopped"):
            return "stopped"
        return "failed"

    def is_running(self, gid: str) -> bool:
        process = self._procs.get(gid)
        if process:
            return process.poll() is None
        goal = self.store.get_goal(gid)
        return bool(goal and goal.get("state") in ("running", "stopping")
                    and self._pid_alive(goal.get("pid")))

    def start(self, gid: str, command=None) -> dict:
        with self._lock:
            goal = self.store.get_goal(gid)
            if not goal:
                raise ValueError("unknown goal")
            if self.is_running(gid):
                return goal
            cmd = command or (self.command_factory(goal) if self.command_factory else None)
            cmd = cmd or [sys.executable, "-m", "parley.cli", "run", "--config", goal["config_path"]]
            process = subprocess.Popen(cmd, cwd=str(_REPO))
            self._procs[gid] = process
            return self.store.update_goal(gid, state="running", pid=process.pid, stop_reason="")

    def stop(self, gid: str, force: bool = False) -> dict:
        goal = self.store.get_goal(gid)
        if not goal:
            raise ValueError("unknown goal")
        control = Path(goal["data_dir"]) / "control.json"
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(json.dumps({"seq": int(time.time()), "verdict": "stop"}), encoding="utf-8")
        process = self._procs.get(gid)
        if force and process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        return self.store.update_goal(gid, state=("stopped" if force else "stopping"))

    def control(self, gid: str, verdict: str, inject: str = "") -> dict | None:
        goal = self.store.get_goal(gid)
        if not goal:
            return None
        control = Path(goal["data_dir"]) / "control.json"
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(json.dumps({
            "seq": int(time.time()), "verdict": verdict, "inject": inject,
        }), encoding="utf-8")
        return goal

    def refresh(self, gid: str) -> dict | None:
        """Project terminal event is authoritative; missing terminal event fails safe."""
        with self._lock:
            goal = self.store.get_goal(gid)
            if not goal:
                return None
            if goal["state"] not in ("running", "stopping") or self.is_running(gid):
                return goal
            reason = self._terminal_reason(goal)
            if goal["state"] == "stopping" and not reason:
                reason = "stopped"
            goal = self.store.update_goal(
                gid, state=self._state_for_reason(reason), pid=0,
                stop_reason=(reason or "orphaned"),
            )
            self._procs.pop(gid, None)
            return goal

    def project_status(self, pid: str, tick: bool = True) -> dict:
        if tick:
            self.tick_project(pid)
        project = self.store.get_project(pid)
        if not project:
            raise ValueError("unknown project")
        return project.get("run") or {"state": "idle", "active_goal_id": None}

    def start_project(self, pid: str) -> dict:
        with self._lock:
            project = self.store.get_project(pid)
            if not project:
                raise ValueError("unknown project")
            contract = project.get("contract") or {}
            if not contract.get("approved"):
                raise ValueError("contract not approved")
            if contract.get("execution_mode", "sequential") != "sequential":
                raise ValueError("parallel requires isolated worktrees")
            goal_ids = contract.get("goal_ids") or []
            if not goal_ids:
                raise ValueError("contract has no goals")
            for gid in goal_ids:
                goal = self.store.get_goal(gid)
                if goal and goal.get("state") == "idle":
                    self.store.update_goal(gid, state="queued")
            self.store.update_project(pid, run={
                "state": "running",
                "active_goal_id": None,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "stop_reason": None,
            })
            self.tick_project(pid)
            self._ensure_project_worker(pid)
            return self.project_status(pid, tick=False)

    def stop_project(self, pid: str) -> dict:
        with self._lock:
            project = self.store.get_project(pid)
            if not project:
                raise ValueError("unknown project")
            run = project.get("run") or {}
            active = run.get("active_goal_id")
            active_goal = self.store.get_goal(active) if active else None
            if active_goal and active_goal.get("state") == "running":
                self.stop(active)
            for gid in (project.get("contract") or {}).get("goal_ids", []):
                goal = self.store.get_goal(gid)
                if goal and goal.get("state") == "queued":
                    self.store.update_goal(gid, state="idle")
            run.update(state="stopped", active_goal_id=active, stop_reason="observer")
            self.store.update_project(pid, run=run)
            return run

    def resume_project(self, pid: str) -> dict:
        """Resume a blocked/stopped project.

        When a goal blocks the runner (stopped/failed/needs_human/paused), its
        worker thread has exited and its process is dead, so writing control.json
        reaches nobody. Resume re-queues the blocking goal, clears its stale
        control.json (so the old stop/pause verdict doesn't immediately re-fire),
        flips run->running, and restarts the project worker.
        """
        with self._lock:
            project = self.store.get_project(pid)
            if not project:
                raise ValueError("unknown project")
            run = project.get("run") or {}
            if run.get("state") not in ("blocked", "stopped"):
                raise ValueError(f"project not resumable (state={run.get('state')})")
            blocked_gid = run.get("active_goal_id")
            if blocked_gid:
                goal = self.store.get_goal(blocked_gid)
                if goal:
                    control = Path(goal["data_dir"]) / "control.json"
                    try:
                        control.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass
                    if goal.get("state") in ("stopped", "failed", "needs_human", "paused"):
                        self.store.update_goal(blocked_gid, state="queued", pid=0, stop_reason="")
            run.update(state="running", active_goal_id=None, stop_reason=None,
                       resumed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            self.store.update_project(pid, run=run)
            self.tick_project(pid)
            self._ensure_project_worker(pid)
            return self.project_status(pid, tick=False)

    def tick_project(self, pid: str) -> dict:
        with self._lock:
            project = self.store.get_project(pid)
            if not project:
                raise ValueError("unknown project")
            run = project.get("run") or {"state": "idle", "active_goal_id": None}
            if run.get("state") != "running":
                return run
            goal_ids = (project.get("contract") or {}).get("goal_ids") or []
            goals = [self.refresh(gid) for gid in goal_ids if self.store.get_goal(gid)]
            active = next((g for g in goals if g.get("state") in ("running", "stopping")), None)
            if active:
                run["active_goal_id"] = active["id"]
                self.store.update_project(pid, run=run)
                return run
            blocked = next((g for g in goals if g.get("state") in
                            ("stopped", "failed", "needs_human", "paused")), None)
            if blocked:
                run.update(state="blocked", active_goal_id=blocked["id"],
                           stop_reason=blocked.get("stop_reason") or blocked["state"])
                self.store.update_project(pid, run=run)
                return run
            next_goal = next((g for g in goals if g.get("state") in ("idle", "queued")), None)
            if next_goal:
                started = self.start(next_goal["id"])
                run["active_goal_id"] = started["id"]
                self.store.update_project(pid, run=run)
                return run
            run.update(state="done", active_goal_id=None, stop_reason="done")
            self.store.update_project(pid, run=run)
            return run

    def _ensure_project_worker(self, pid: str) -> None:
        thread = self._project_threads.get(pid)
        if thread and thread.is_alive():
            return
        thread = threading.Thread(target=self._project_worker, args=(pid,), daemon=True)
        self._project_threads[pid] = thread
        thread.start()

    def _project_worker(self, pid: str) -> None:
        while True:
            try:
                run = self.tick_project(pid)
            except Exception:
                return
            if run.get("state") != "running":
                return
            time.sleep(self.poll_interval)
