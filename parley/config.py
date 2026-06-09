"""Parley typed config (mục 6.1)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Limits:
    max_turns: int = 200
    hard_iter_cap: int = 500       # R2-10
    max_turn_errors: int = 5       # R2-10
    idle_timeout_s: int = 120
    hard_timeout_s: int = 1800
    max_executor_errors: int = 3
    max_warm_turns_per_phase: int = 20   # ADR-14: force cold seed after N warm turns (safety)


@dataclass
class Verify:
    timeout_s: int = 600           # ADR-09
    gates: list = field(default_factory=list)   # R2-4: PASS = all exit 0


@dataclass
class Role:
    cmd: list
    edit: bool = False
    fallbacks: list = field(default_factory=list)


@dataclass
class Git:
    branch: str = "parley/work"
    auto_push: bool = False            # ADR-11: push opt-in
    push_on: str = "complete"          # never | complete
    remote: str = "origin"
    protected: list = field(default_factory=lambda: ["main", "master", "develop"])


@dataclass
class AdvisorChat:
    """Warm Advisor session cho Init (ADR-13). exec resume KHÔNG nhận --sandbox →
    giữ read-only bằng -c 'sandbox_mode="read-only"'. --json để lấy thread_id; '-' đọc stdin."""
    mode: str = "warm"
    start_cmd: list = field(default_factory=lambda: [
        "codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
        "-c", 'model_reasoning_effort="low"', "-"])
    resume_cmd: list = field(default_factory=lambda: [
        "codex", "exec", "resume", "--json", "--skip-git-repo-check",
        "-c", 'sandbox_mode="read-only"', "-c", 'model_reasoning_effort="low"', "{session_id}", "-"])


@dataclass
class Housekeeping:
    """ADR-12: side-channel document/git. Artifacts ghi vào project_dir (KHÔNG vào ~/.parley).
    document/git dùng CLI của `from_role` (coder) + `model` tối thiểu (rẻ)."""
    enabled: bool = False
    model: str = ""                    # minimal model cho document/git (provider của coder)
    from_role: str = "coder"           # lấy command-profile/provider từ role này
    changelog_path: str = "CHANGELOG.md"   # tương đối project_dir


@dataclass
class Config:
    project_dir: str
    data_dir: str = "./data"
    contract_path: str | None = None
    pipeline_mode: str = "free"    # free | strict (ADR-03)
    every_n_turns: int = 1         # R2-5
    advisor_cmd: list = field(default_factory=lambda: [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"])   # non-interactive; chạy được ở dir chưa trusted
    supervisor_cmd: list = field(default_factory=lambda: [
        "kiro-cli", "chat", "--no-interactive", "--agent", "supervisor", "--trust-tools="])   # judge: read-only, JSON-only (ADR-06)
    roles: dict = field(default_factory=dict)   # name -> Role
    # ADR-15: quota fallback chains for advisor/supervisor (list of profile dicts,
    # same shape as Role.fallbacks). Empty = no fallback (primary only).
    advisor_fallbacks: list = field(default_factory=list)
    supervisor_fallbacks: list = field(default_factory=list)
    cursor_agent_path: str | None = None   # ADR-15: absolute path to cursor-agent (uvicorn PATH may miss it)
    # ADR-14: warm-per-phase sessions for the PRIMARY advisor/supervisor (codex/kiro).
    # When on, turn 1 of a phase cold-seeds via *_warm_start_cmd, later turns resume via
    # *_warm_resume_cmd ("{session_id}" substituted) + a delta prompt. Falls back to cold
    # automatically on PHASE change, resume failure, or any ADR-15 provider fallback.
    advisor_warm: bool = True
    # ADR-14: supervisor warm uses a kiro --list-sessions diff to learn the session id,
    # which is unreliable across the multi-process runner (each goal is a separate
    # process; two goals sharing a cwd can diff the wrong id). Token saving is tiny
    # (supervisor prompt is short), so warm is OFF by default until a multi-process-safe
    # id resolution exists. advisor warm (codex thread_id from JSONL) stays ON.
    supervisor_warm: bool = False
    advisor_warm_start_cmd: list = field(default_factory=lambda: [
        "codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "-"])
    advisor_warm_resume_cmd: list = field(default_factory=lambda: [
        "codex", "exec", "resume", "--json", "--skip-git-repo-check",
        "-c", 'sandbox_mode="read-only"', "{session_id}", "-"])
    # kiro resume: same base cmd + --resume-id <id>; id learned via --list-sessions diff.
    supervisor_warm_resume_cmd: list = field(default_factory=lambda: [
        "kiro-cli", "chat", "--no-interactive", "--agent", "supervisor",
        "--trust-tools=", "--resume-id", "{session_id}"])
    limits: Limits = field(default_factory=Limits)
    verify: Verify = field(default_factory=Verify)
    git: Git = field(default_factory=Git)
    housekeeping: Housekeeping = field(default_factory=Housekeeping)
    advisor_chat: AdvisorChat = field(default_factory=AdvisorChat)
