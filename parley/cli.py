"""Parley CLI (mục 16): `parley init <folder> --goal "..."` and `parley run`.

init = deterministic bootstrap (no LLM shell, ADR-06): detect stack, sync local
agents, git branch, write config. run = wire real CLIs into the harness loop.
Config persisted as JSON (parley.config.json) to stay dependency-free.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import executor
from .backends import CommandProfile, GenericCliBackend, SubprocessVerifyRunner
from .channel import Channel
from .config import Config, Git, Housekeeping, Limits, Role, Verify
from .harness import Harness

_CANONICAL_AGENTS = Path(__file__).resolve().parent.parent / ".kiro" / "agents"
_KIRO = ["kiro-cli", "chat", "--no-interactive", "--agent"]


def detect_gates(pd: Path) -> list:
    g = []
    if (pd / "package.json").exists():
        pm = ("pnpm" if (pd / "pnpm-lock.yaml").exists()
              else "yarn" if (pd / "yarn.lock").exists() else "npm")
        g += [[pm, "run", "build"], [pm, "test"]]
    if (pd / "Cargo.toml").exists():
        g.append(["cargo", "test"])
    if (pd / "pyproject.toml").exists() or (pd / "requirements.txt").exists():
        g.append(["python", "-m", "pytest", "-q"])
    if (pd / "go.mod").exists():
        g.append(["go", "test", "./..."])
    return g


def _claude_fb(role_agent: str, edit: bool, model: str) -> dict:
    """Claude profile. read-only -> --permission-mode plan; edit -> skip-permissions.
    max_attempts=3: retry on rate_limit before moving to the next profile (ADR-15)."""
    cmd = ["claude", "-p", "--agent", role_agent, "--model", model, "--output-format", "text"]
    cmd += (["--dangerously-skip-permissions"] if edit else ["--permission-mode", "plan"])
    return {"provider": "claude", "model": model, "cmd": cmd, "max_attempts": 3, "input": "stdin"}


def _opencode_fb(agent: str, model: str) -> dict:
    return {"provider": "opencode", "model": model,
            "cmd": ["opencode", "run", "--agent", agent, "--model", model],
            "max_attempts": 1, "input": "stdin"}


def _cursor_fb(edit: bool) -> dict:
    """Cursor reads the prompt via argv (no stdin in print mode). read-only -> plan +
    thinking model; edit -> --force + auto. cmd[0] may be overridden by cursor_agent_path."""
    if edit:
        cmd = ["cursor-agent", "-p", "--trust", "--force", "--model", "auto", "--output-format", "text"]
        model = "auto"
    else:
        cmd = ["cursor-agent", "-p", "--trust", "--mode", "plan",
               "--model", "claude-opus-4-8-thinking-high", "--output-format", "text"]
        model = "claude-opus-4-8-thinking-high"
    return {"provider": "cursor", "model": model, "cmd": cmd, "max_attempts": 1, "input": "argv_prompt"}


# Claude named-agent mapping (safe names that exist in ~/.claude/agents; preflight
# in executor falls through to cli_unavailable if missing).
_CLAUDE_AGENT = {"analyzer": "code-analyzer", "architect": "architecture",
                 "researcher": "researcher", "reviewer": "reviewer",
                 "coder": "coder", "fixer": "fixer"}


def default_roles() -> dict:
    """ADR-15 verified routing. Primary = kiro (read-only roles) / opencode (code roles);
    fallbacks switch provider on quota/account errors, cursor as last resort."""
    def read_only(role: str, oc_primary: str | None = None) -> dict:
        # researcher/analyzer/architect: kiro -> claude 4.8 -> claude 4.7 -> cursor.
        # LS-020 (hướng B): read-only roles cần GHI report file -> phải write-capable ở
        # tầng CLI (kiro --trust-all-tools, claude --dangerously-skip-permissions, cursor
        # --force). Ràng buộc "không sửa source" chuyển sang MỀM: SCOPE trong role prompt
        # (context.role_prompt_document) + advisor review diff. Cờ role `edit` vẫn = False
        # (harness không feed diff/verify như edit role), nhưng CLI được phép ghi.
        return {
            "cmd": _KIRO + [role, "--trust-all-tools"], "edit": False,
            "fallbacks": [
                _claude_fb(_CLAUDE_AGENT[role], True, "claude-opus-4.8"),
                _claude_fb(_CLAUDE_AGENT[role], True, "claude-opus-4.7"),
                _cursor_fb(True),
            ],
        }

    return {
        "analyzer":  read_only("analyzer"),
        "architect": read_only("architect"),
        "researcher": read_only("researcher"),
        # reviewer: read-only role nhưng cần GHI report (hướng B) -> opencode `build`
        # (write-capable) thay vì `plan`. SCOPE prompt cấm sửa source.
        "reviewer": {
            "cmd": ["opencode", "run", "--agent", "build", "--model", "opencode-go/qwen3.7-max"],
            "edit": False,
            "fallbacks": [
                _opencode_fb("build", "opencode-go/glm-5.1"),
                _opencode_fb("build", "opencode-go/qwen3.7-plus"),
                _cursor_fb(True),
            ],
        },
        # coder: opencode build/qwen3.7-plus -> minimax-m3 -> glm-5.1 -> cursor auto
        "coder": {
            "cmd": ["opencode", "run", "--agent", "build", "--model", "opencode-go/qwen3.7-plus"],
            "edit": True,
            "fallbacks": [
                _opencode_fb("build", "opencode-go/minimax-m3"),
                _opencode_fb("build", "opencode-go/glm-5.1"),
                _cursor_fb(True),
            ],
        },
        # fixer: opencode build/qwen3.7-max -> qwen3.7-plus -> minimax-m3 -> glm-5.1 -> cursor auto
        "fixer": {
            "cmd": ["opencode", "run", "--agent", "build", "--model", "opencode-go/qwen3.7-max"],
            "edit": True,
            "fallbacks": [
                _opencode_fb("build", "opencode-go/qwen3.7-plus"),
                _opencode_fb("build", "opencode-go/minimax-m3"),
                _opencode_fb("build", "opencode-go/glm-5.1"),
                _cursor_fb(True),
            ],
        },
    }


def default_advisor_fallbacks() -> list:
    """ADR-15: advisor (primary codex) falls back to a strong, broad-context model
    from a DIFFERENT family than the executors (qwen/minimax) so a single provider's
    quota exhaustion doesn't take down advisor + executors together."""
    return [{"provider": "opencode", "model": "opencode-go/kimi-k2.6",
             "cmd": ["opencode", "run", "--agent", "plan", "--model", "opencode-go/kimi-k2.6"],
             "max_attempts": 1, "input": "stdin"}]


def default_supervisor_fallbacks() -> list:
    """ADR-15: supervisor only emits a short JSON verdict — a fast/cheap model is
    enough; runs every dispatch so cost matters more than raw power."""
    return [{"provider": "opencode", "model": "opencode-go/deepseek-v4-flash",
             "cmd": ["opencode", "run", "--agent", "plan", "--model", "opencode-go/deepseek-v4-flash"],
             "max_attempts": 1, "input": "stdin"}]


def sync_agents(project_dir: Path) -> int:
    dst = project_dir / ".kiro" / "agents"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    if _CANONICAL_AGENTS.exists():
        for f in _CANONICAL_AGENTS.glob("*.json"):
            shutil.copy2(f, dst / f.name)
            n += 1
    gi = project_dir / ".gitignore"
    line = ".kiro/agents/"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if line not in existing:
        gi.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n")
                      + line + "\n", encoding="utf-8")
    return n


def project_init(project_dir: str, branch: str = "parley/work") -> dict:
    """PROJECT-level setup (1 lần): detect stack, sync .kiro/agents, git branch. Trả gates."""
    pd = Path(project_dir).resolve()
    pd.mkdir(parents=True, exist_ok=True)
    gates = detect_gates(pd)
    sync_agents(pd)
    subprocess.run(["git", "checkout", "-B", branch], cwd=str(pd), capture_output=True)
    return {"project_dir": str(pd), "gates": gates}


def build_goal_config(project_dir: str, goal: str | None, gates: list,
                      config_path: str = "parley.config.json", data_dir: str = "./data",
                      contract_path: str | None = None) -> dict:
    """GOAL-level (rẻ): ghi config kế thừa gates của project. KHÔNG detect/sync lại."""
    pd = Path(project_dir).resolve()
    cfg_file = Path(config_path)
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    cfg.setdefault("project_dir", str(pd))
    cfg.setdefault("data_dir", data_dir)
    cfg.setdefault("contract_path", None)
    if contract_path:
        cfg["contract_path"] = contract_path
    cfg.setdefault("first_phase", 1)
    cfg.setdefault("pipeline_mode", "free")
    cfg.setdefault("every_n_turns", 1)
    if goal or "goal" not in cfg:                      # idempotent: keep running goal unless given
        cfg["goal"] = goal or cfg.get("goal", "")
    cfg["advisor_cmd"] = cfg.get("advisor_cmd") or Config(project_dir=str(pd)).advisor_cmd
    cfg["supervisor_cmd"] = cfg.get("supervisor_cmd") or Config(project_dir=str(pd)).supervisor_cmd
    if "advisor_fallbacks" not in cfg:
        cfg["advisor_fallbacks"] = default_advisor_fallbacks()
    if "supervisor_fallbacks" not in cfg:
        cfg["supervisor_fallbacks"] = default_supervisor_fallbacks()
    cfg["roles"] = cfg.get("roles") or default_roles()
    cfg["limits"] = cfg.get("limits") or Limits().__dict__
    cfg["verify"] = cfg.get("verify") or {"timeout_s": 600, "gates": gates}
    cfg["git"] = cfg.get("git") or Git().__dict__
    cfg["housekeeping"] = cfg.get("housekeeping") or Housekeeping().__dict__
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def init(project_dir: str, goal: str | None, config_path: str = "parley.config.json",
         data_dir: str = "./data", branch: str = "parley/work") -> dict:
    """Wrapper backward-compat: project_init + build_goal_config."""
    res = project_init(project_dir, branch)
    return build_goal_config(project_dir, goal, res["gates"], config_path, data_dir)


def load_config(config_path: str) -> tuple[Config, str, int, str | None]:
    d = json.loads(Path(config_path).read_text(encoding="utf-8"))
    defaults = default_roles()
    roles = {k: Role(cmd=v["cmd"], edit=v.get("edit", False),
                     fallbacks=v.get("fallbacks", defaults.get(k, {}).get("fallbacks", [])))
             for k, v in d["roles"].items()}
    cfg = Config(
        project_dir=d["project_dir"], data_dir=d.get("data_dir", "./data"),
        contract_path=d.get("contract_path"), pipeline_mode=d.get("pipeline_mode", "free"),
        every_n_turns=d.get("every_n_turns", 1),
        advisor_cmd=d["advisor_cmd"], supervisor_cmd=d["supervisor_cmd"], roles=roles,
        advisor_fallbacks=d.get("advisor_fallbacks", default_advisor_fallbacks()),
        supervisor_fallbacks=d.get("supervisor_fallbacks", default_supervisor_fallbacks()),
        limits=Limits(**d.get("limits", {})), verify=Verify(**d.get("verify", {})),
        git=Git(**d.get("git", {})), housekeeping=Housekeeping(**d.get("housekeeping", {})),
        cursor_agent_path=d.get("cursor_agent_path"),
        **{k: d[k] for k in ("advisor_warm", "supervisor_warm", "advisor_warm_start_cmd",
                             "advisor_warm_resume_cmd", "supervisor_warm_resume_cmd") if k in d})
    contract = ""
    if cfg.contract_path and Path(cfg.contract_path).exists():
        contract = Path(cfg.contract_path).read_text(encoding="utf-8")
    return cfg, d.get("goal", ""), int(d.get("first_phase", 1)), contract


def _sup_runner(cfg: Config, backend, ch=None):
    """Supervisor runner with ADR-15 quota fallback + ADR-14 warm session. On quota
    failure of the primary (kiro), falls back to cfg.supervisor_fallbacks (opencode);
    non-quota output (incl. a malformed verdict) is returned as-is for supervisor.gate.

    Warm (ADR-14): kiro prints no session id in headless output, so we learn it via a
    --list-sessions diff around the first (seeding) gate, then --resume-id thereafter
    for continuity across gates within a phase. `runner.reset()` forces a fresh session
    (called by the harness on a new phase). Any fallback to a non-kiro provider drops
    the session. Best-effort: an ambiguous diff (0 or >1 new ids) stays cold."""
    emit = (lambda *a, **k: None)
    if ch is not None:
        def emit(etype, **kw):
            ch.emit(etype, **kw)

    warm = bool(cfg.supervisor_warm)
    sess = {"id": None}

    def runner(prompt: str) -> str:
        if not warm:
            out = executor.run_text_with_fallback(
                "supervisor", cfg.supervisor_cmd, cfg.supervisor_fallbacks, backend,
                stdin_input=prompt, cwd=cfg.project_dir,
                idle_timeout=cfg.limits.idle_timeout_s, hard_timeout=cfg.limits.hard_timeout_s,
                cursor_agent_path=cfg.cursor_agent_path, emit=emit, log_dir=cfg.data_dir)
            return out.stdout
        # warm: snapshot kiro sessions before the first (seeding) gate to learn the id.
        before = executor.kiro_session_ids(backend, cfg.project_dir) if sess["id"] is None else None
        out = executor.run_text_with_fallback(
            "supervisor", cfg.supervisor_cmd, cfg.supervisor_fallbacks, backend,
            stdin_input=prompt, cwd=cfg.project_dir,
            idle_timeout=cfg.limits.idle_timeout_s, hard_timeout=cfg.limits.hard_timeout_s,
            cursor_agent_path=cfg.cursor_agent_path, emit=emit, log_dir=cfg.data_dir,
            warm=True, session_id=sess["id"], warm_start_cmd=cfg.supervisor_cmd,
            warm_resume_cmd=cfg.supervisor_warm_resume_cmd, warm_stdin=prompt)
        if out.provider != "kiro":
            sess["id"] = None                         # fell back -> session invalid
        elif sess["id"] is None and before is not None:
            after = executor.kiro_session_ids(backend, cfg.project_dir)
            new = after - before
            sess["id"] = next(iter(new)) if len(new) == 1 else None   # ambiguous -> stay cold
        return out.stdout

    runner.reset = lambda: sess.update(id=None)        # harness calls on new phase
    return runner


def run(config_path: str = "parley.config.json") -> str | None:
    cfg, goal, first_phase, contract = load_config(config_path)
    ch = Channel(cfg.data_dir)
    backend = GenericCliBackend()
    h = Harness(cfg, ch, backend, SubprocessVerifyRunner(), _sup_runner(cfg, backend, ch))
    return h.run(goal, contract or cfg.contract_path, first_phase)


def main(argv=None):
    p = argparse.ArgumentParser(prog="parley")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init")
    pi.add_argument("project_dir")
    pi.add_argument("--goal", default=None)
    pi.add_argument("--config", default="parley.config.json")
    pr = sub.add_parser("run")
    pr.add_argument("--config", default="parley.config.json")
    a = p.parse_args(argv)
    if a.cmd == "init":
        cfg = init(a.project_dir, a.goal, a.config)
        print(f"init ok -> {a.config}; gates={cfg['verify']['gates']}")
    elif a.cmd == "run":
        print(f"stopped: {run(a.config)}")


if __name__ == "__main__":
    sys.exit(main())
