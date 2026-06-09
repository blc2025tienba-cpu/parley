"""Parley multi-project/goal registry.

Home:
  registry.json
  configs/<goal_id>.json
  data/<goal_id>/...
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import cli
from . import planner


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def home_dir() -> Path:
    return Path(os.environ.get("PARLEY_HOME") or (Path.home() / ".parley"))


class Store:
    def __init__(self, home=None):
        self.home = Path(home) if home else home_dir()
        (self.home / "configs").mkdir(parents=True, exist_ok=True)
        (self.home / "data").mkdir(parents=True, exist_ok=True)
        self.reg_f = self.home / "registry.json"
        self._lock = threading.RLock()
        self.reg = self._load()
        self._migrate_contract_paths()

    def _load(self) -> dict:
        if self.reg_f.exists():
            try:
                return json.loads(self.reg_f.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"projects": {}, "goals": {}}

    def _save(self) -> None:
        tmp = self.reg_f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.reg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.reg_f)

    def _project_contract_path(self, p: dict) -> str | None:
        cp = p.get("contract_path")
        if cp and Path(cp).exists():
            return str(Path(cp).resolve())
        root = Path(p["project_dir"]).resolve()
        for rec in reversed(p.get("artifacts") or []):
            rel = str(rec.get("path", ""))
            if rel.replace("\\", "/").endswith("/domain-contract.md") or rel == "domain-contract.md":
                candidate = (root / rel).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if candidate.exists():
                    return str(candidate)
        return None

    def _write_goal_contract_path(self, goal: dict, contract_path: str) -> bool:
        cf = Path(goal.get("config_path", ""))
        if not cf.exists():
            return False
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            return False
        if d.get("contract_path") == contract_path:
            return False
        d["contract_path"] = contract_path
        cf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def _migrate_contract_paths(self) -> None:
        changed = False
        for p in self.reg.get("projects", {}).values():
            cp = self._project_contract_path(p)
            if not cp:
                continue
            if p.get("contract_path") != cp:
                p["contract_path"] = cp
                changed = True
            for g in self.reg.get("goals", {}).values():
                if g.get("project_id") == p["id"] and g.get("state") != "running":
                    changed = self._write_goal_contract_path(g, cp) or changed
        if changed:
            self._save()

    # ---- projects ----
    def add_project(self, name: str, project_dir: str) -> dict:
        with self._lock:
            pid = _id("prj")
            self.reg["projects"][pid] = {
                "id": pid, "name": name,
                "project_dir": str(Path(project_dir).resolve()), "created_at": _now(),
            }
            self._save()
            return self.reg["projects"][pid]

    def list_projects(self) -> list:
        return list(self.reg["projects"].values())

    def get_project(self, pid: str):
        return self.reg["projects"].get(pid)

    def update_project(self, pid: str, **fields) -> dict | None:
        with self._lock:
            p = self.reg["projects"].get(pid)
            if not p:
                return None
            p.update({k: v for k, v in fields.items() if v is not None})
            self._save()
            return p

    def init_project(self, project_id: str) -> dict:
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p:
                raise ValueError("unknown project")
            res = cli.project_init(p["project_dir"])
            p["initialized"] = True
            p["gates"] = res["gates"]
            self._save()
            return p

    def add_chat(self, project_id: str, role: str, text: str) -> list:
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p:
                raise ValueError("unknown project")
            p.setdefault("chat", []).append({"role": role, "text": text, "ts": _now()})
            self._save()
            return p["chat"]

    def get_chat(self, project_id: str) -> list:
        p = self.reg["projects"].get(project_id)
        return (p or {}).get("chat", [])

    def plan_project(self, project_id: str, ideas, runner) -> dict:
        contract = planner.plan(ideas, runner)
        contract["ideas"] = ideas if isinstance(ideas, list) else [str(ideas)]
        return self.set_contract_draft(project_id, contract)

    def set_contract_draft(self, project_id: str, contract: dict) -> dict:
        """Create an editable draft; keep approved contracts in history for audit."""
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p:
                raise ValueError("unknown project")
            current = p.get("contract") or {}
            if current.get("approved"):
                p.setdefault("contract_history", []).append(current.copy())
                base_goal_ids = list(current.get("goal_ids") or [])
            else:
                base_goal_ids = list(current.get("base_goal_ids") or [])
            mode = contract.get("execution_mode", "sequential")
            if mode not in ("sequential", "parallel"):
                mode = "sequential"
            goals = []
            seen_titles = set()
            for item in contract.get("goals") or []:
                title = item if isinstance(item, str) else item.get("title", "")
                title = str(title).strip()
                description = "" if isinstance(item, str) else str(item.get("description", "")).strip()
                key = title.casefold()
                if title and key not in seen_titles:
                    goals.append({"title": title[:500], "description": description[:12000]})
                    seen_titles.add(key)
            draft = {
                "execution_mode": mode,
                "goals": goals,
                "reason": str(contract.get("reason", "")),
                "approved": False,
                "base_goal_ids": base_goal_ids,
                "drafted_at": _now(),
            }
            if contract.get("ideas") is not None:
                draft["ideas"] = contract["ideas"]
            p["contract"] = draft
            self._save()
            return draft

    def approve_contract(self, project_id: str, strategy: str = "replace") -> list:
        """Approve a draft into real goals.

        replace: supersede old idle/queued goals but keep their data for audit.
        append: keep old contract goals and add the new ones after them.
        """
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p or not p.get("contract"):
                raise ValueError("no contract")
            c = p["contract"]
            if c.get("approved"):
                return []
            if strategy not in ("replace", "append"):
                raise ValueError("strategy must be replace or append")
            if not c.get("goals"):
                raise ValueError("contract has no goals")
            base_goal_ids = list(c.get("base_goal_ids") or [])
            goal_specs = list(c["goals"])
            if strategy == "append":
                existing_titles = {
                    str(self.get_goal(gid).get("goal", "")).strip().casefold()
                    for gid in base_goal_ids if self.get_goal(gid)
                }
                goal_specs = [g for g in goal_specs if g["title"].strip().casefold() not in existing_titles]
            created = [self.add_goal(project_id, g["title"], g.get("description", "")) for g in goal_specs]
            if strategy == "replace":
                for gid in base_goal_ids:
                    old = self.get_goal(gid)
                    if old and old.get("state") in ("idle", "queued"):
                        self.update_goal(gid, state="superseded", superseded_at=_now())
                goal_ids = [g["id"] for g in created]
            else:
                goal_ids = [gid for gid in base_goal_ids if self.get_goal(gid)] + [g["id"] for g in created]
            c["approved"] = True
            c["approval_strategy"] = strategy
            c["approved_at"] = _now()
            c["goal_ids"] = goal_ids
            c.pop("base_goal_ids", None)
            self._save()
            return created

    def save_artifact(self, project_id: str, relative_path: str, content: str) -> dict:
        """Write an end-user-approved text artifact inside project_dir."""
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p:
                raise ValueError("unknown project")
            rel = Path(str(relative_path or "").strip())
            if not str(rel) or rel.is_absolute() or ".." in rel.parts:
                raise ValueError("artifact path must be relative and inside project")
            root = Path(p["project_dir"]).resolve()
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise ValueError("artifact path escapes project")
            if target.suffix.lower() not in (".md", ".txt", ".json", ".yaml", ".yml"):
                raise ValueError("artifact must be a text document")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(str(content), encoding="utf-8")
            os.replace(tmp, target)
            record = {"path": str(rel).replace("\\", "/"), "saved_at": _now()}
            p.setdefault("artifacts", []).append(record)
            if target.name == "domain-contract.md":
                p["contract_path"] = str(target)
                for g in self.list_goals(project_id):
                    if g.get("state") != "running":
                        self._write_goal_contract_path(g, p["contract_path"])
            self._save()
            return record

    # ---- goals ----
    def add_goal(self, project_id: str, goal_text: str, description: str = "") -> dict:
        with self._lock:
            p = self.reg["projects"].get(project_id)
            if not p:
                raise ValueError("unknown project")
            if not p.get("initialized"):
                self.init_project(project_id)
                p = self.reg["projects"][project_id]
            gid = _id("goal")
            data_dir = self.home / "data" / gid
            cfg_path = self.home / "configs" / f"{gid}.json"
            full_goal = str(goal_text)
            desc = str(description or "").strip()
            if desc:
                full_goal = f"{full_goal}\n\n# Goal Details\n{desc}"
            cli.build_goal_config(
                p["project_dir"], full_goal, p.get("gates", []),
                str(cfg_path), str(data_dir), contract_path=self._project_contract_path(p),
            )
            g = {
                "id": gid, "project_id": project_id, "goal": goal_text,
                "description": desc,
                "state": "idle", "created_at": _now(),
                "config_path": str(cfg_path), "data_dir": str(data_dir), "pid": None,
            }
            self.reg["goals"][gid] = g
            self._save()
            return g

    def list_goals(self, project_id=None) -> list:
        return [
            g for g in self.reg["goals"].values()
            if project_id is None or g["project_id"] == project_id
        ]

    def get_goal(self, gid: str):
        return self.reg["goals"].get(gid)

    def update_goal(self, gid: str, **fields) -> dict | None:
        with self._lock:
            g = self.reg["goals"].get(gid)
            if not g:
                return None
            g.update({k: v for k, v in fields.items() if v is not None})
            self._save()
            # Rebuild the config's goal field (title + description) whenever either changes,
            # mirroring add_goal so a live run picks up the edited text.
            if fields.get("goal") is not None or fields.get("description") is not None:
                cf = Path(g["config_path"])
                if cf.exists():
                    d = json.loads(cf.read_text(encoding="utf-8"))
                    full_goal = str(g.get("goal", ""))
                    desc = str(g.get("description") or "").strip()
                    if desc:
                        full_goal = f"{full_goal}\n\n# Goal Details\n{desc}"
                    d["goal"] = full_goal
                    cf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            return g

    def delete_goal(self, gid: str) -> dict | None:
        """Soft-delete: mark state='deleted' but keep the registry entry + data_dir
        for audit. Refuse to delete a goal that is still running/stopping."""
        with self._lock:
            g = self.reg["goals"].get(gid)
            if not g:
                return None
            if g.get("state") in ("running", "stopping"):
                raise ValueError("cannot delete a running goal; stop it first")
            g["state"] = "deleted"
            g["deleted_at"] = _now()
            self._save()
            return g

    def read_config(self, gid: str) -> dict | None:
        g = self.get_goal(gid)
        if not g or not Path(g["config_path"]).exists():
            return None
        return json.loads(Path(g["config_path"]).read_text(encoding="utf-8"))

    def write_config(self, gid: str, cfg: dict) -> bool:
        with self._lock:
            g = self.get_goal(gid)
            if not g:
                return False
            Path(g["config_path"]).write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            if cfg.get("goal"):
                g["goal"] = cfg["goal"]
                self._save()
            return True
