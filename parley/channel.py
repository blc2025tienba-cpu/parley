"""Parley channel: owns the 4 data files + report snapshots (ADR-05/08, R2-2).

conversation.ndjson / decisions.ndjson are append-only (UTF-8, \\n).
status.json / control.json are single-object; status written atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Snap:
    path: str
    sha: str


@dataclass
class State:
    phase: object
    turn: int
    pending_dispatch: bool


class Channel:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        (self.dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(parents=True, exist_ok=True)
        self.conv = self.dir / "conversation.ndjson"
        self.dec = self.dir / "decisions.ndjson"
        self.status_f = self.dir / "status.json"
        self.control_f = self.dir / "control.json"
        self._id = self._max_id(self.conv)
        self._did = self._max_id(self.dec)

    @staticmethod
    def _max_id(f: Path) -> int:
        n = 0
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    n = max(n, int(json.loads(line).get("id", 0)))
                except Exception:
                    pass
        return n

    @staticmethod
    def _append(f: Path, obj: dict):
        with f.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def emit(self, type: str, **kw) -> dict:
        self._id += 1
        obj = {"id": self._id, "ts": _now(), "type": type, **kw}
        self._append(self.conv, obj)
        return obj

    def log_decision(self, **kw) -> dict:
        self._did += 1
        obj = {"id": self._did, "ts": _now(), **kw}
        self._append(self.dec, obj)
        return obj

    def update_status(self, status: dict):
        tmp = self.status_f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.status_f)  # atomic on same volume

    def read_control(self):
        if not self.control_f.exists():
            return None
        try:
            return json.loads(self.control_f.read_text(encoding="utf-8"))
        except Exception:
            return None

    def snapshot_report(self, src_path: str) -> Snap:
        data = Path(src_path).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        dst = self.dir / "reports" / f"{sha[:8]}-{Path(src_path).name}"
        dst.write_bytes(data)
        return Snap(str(dst), sha)

    def write_prompt(self, task_id: str, content: str, project_dir: str | None = None) -> Snap:
        """Materialize the role prompt. Always keep an audit copy in data_dir. When
        project_dir is given, ALSO write a copy inside it (`.parley/prompts/`) and return
        THAT path: sandboxed providers (claude/cursor) can only read files within the
        workspace (project_dir), so a data_dir path (~/.parley/data/...) is unreadable to
        them -> missing_report. The in-workspace copy is the path handed to the executor."""
        audit = self.dir / "prompts" / f"{task_id}.md"
        audit.write_text(content, encoding="utf-8")
        sha = hashlib.sha256(audit.read_bytes()).hexdigest()
        if project_dir:
            local = Path(project_dir) / ".parley" / "prompts" / f"{task_id}.md"
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(content, encoding="utf-8")
            return Snap(str(local.resolve()), sha)
        return Snap(str(audit.resolve()), sha)

    def tail(self, n: int = 20) -> list:
        if not self.conv.exists():
            return []
        lines = [l for l in self.conv.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [json.loads(l) for l in lines[-n:]]

    def resume(self) -> State | None:
        events = self.tail(10 ** 9)
        if not events:
            return None
        phase, turn = None, 0
        last_dispatch = None
        for e in events:
            if e.get("phase") is not None:
                phase = e["phase"]
            if isinstance(e.get("turn"), int):
                turn = max(turn, e["turn"])
            t = e.get("type")
            if t == "dispatch":
                last_dispatch = e
            elif t == "report" and last_dispatch and e.get("slice") == last_dispatch.get("slice"):
                last_dispatch = None
        return State(phase, turn, last_dispatch is not None)
