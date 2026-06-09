"""Parley executor: failure classification + provider fallback (ADR-15).

The harness must distinguish three executor outcomes that the old code collapsed
into a single `report done=false`:

  1. Agent produced a valid report (REPORT trailer, or wrote the report file).
  2. Executor could not run (rate limit / quota / auth / CLI missing).
  3. Executor ran but produced no report (missing_report / timeout / crash).

Only (1) goes to the Advisor for review. (2) drives provider fallback within the
same task; (3) and non-quota failures stop cleanly so a human is paged instead of
burning quota in a REJECT loop.

This module owns NO governance. It is a thin wrapper around AgentBackend.run_once
that adds: classify(), stale-report protection, a per-role profile chain, and the
per-error fallback policy. The harness consumes ExecOutcome and never sees
transport details.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field

from . import protocol
from .backends import CommandProfile, RunResult

# ---- failure taxonomy --------------------------------------------------------
# Order matters: signatures are checked BEFORE timed_out, because a kiro
# rate-limit retries internally (Retry #2/#3 within 10s) until the idle timeout
# kills it — timed_out=True yet the stdout clearly says "rate limit reached".
# Treating that as `timeout` would fail-fast and lose the fallback opportunity.

RATE_LIMITED = "rate_limited"
USAGE_EXHAUSTED = "usage_exhausted"
AUTH_ERROR = "auth_error"
PERMISSION_DENIED = "permission_denied"
ACCOUNT_SUSPENDED = "account_suspended"
CLI_UNAVAILABLE = "cli_unavailable"
TIMEOUT = "timeout"
CLI_EXIT_ERROR = "cli_exit_error"
MISSING_REPORT = "missing_report"

# Signatures are ordered; first match wins. Context-anchored per user guidance
# (no bare /429/, split auth vs permission vs suspension).
_SIGNATURES: list[tuple[str, re.Pattern]] = [
    (ACCOUNT_SUSPENDED, re.compile(
        r"account (?:is )?(?:suspended|disabled|banned|deactivated)"
        r"|your account has been (?:suspended|disabled)", re.I)),
    (USAGE_EXHAUSTED, re.compile(
        r"quota (?:exceeded|exhausted)|usage limit(?:s)? (?:reached|exceeded)"
        r"|credit balance is too low|out of credits|insufficient[_ ]quota"
        r"|monthly limit reached|you have (?:reached|exceeded) your"
        # cursor-style: "You've hit your usage limit ... usage limits will reset ..."
        r"|hit your usage limit|usage limit(?:s)? will reset|set a spend limit"
        r"|spend limit", re.I)),
    (RATE_LIMITED, re.compile(
        r"HTTP\s*429|status(?:_code)?[=: ]+429|too many requests"
        r"|rate.?limit(?:ed| reached)?|Retry #\d", re.I)),
    (PERMISSION_DENIED, re.compile(
        r"HTTP\s*403|status(?:_code)?[=: ]+403|permission denied|forbidden"
        r"|not authorized to|tool (?:call )?denied", re.I)),
    (AUTH_ERROR, re.compile(
        r"HTTP\s*401|status(?:_code)?[=: ]+401|unauthorized|authentication failed"
        r"|invalid api key|token (?:has )?expired|please (?:re-?)?login|not logged in", re.I)),
    (CLI_UNAVAILABLE, re.compile(
        r"is not recognized as an internal or external command"
        r"|command not found|no such file or directory"
        r"|cannot find the path|executable not found", re.I)),
]

# reason -> fallback action the chain runner applies.
NEXT_PROFILE = "next_profile"     # try the next profile in the chain
SKIP_PROVIDER = "skip_provider"   # drop all remaining profiles of the same provider
STOP = "stop"                     # do not fall back; stop cleanly and page a human

_ACTION = {
    RATE_LIMITED: NEXT_PROFILE,        # after retries are spent
    USAGE_EXHAUSTED: SKIP_PROVIDER,
    AUTH_ERROR: SKIP_PROVIDER,         # this provider can't auth; another might
    ACCOUNT_SUSPENDED: SKIP_PROVIDER,
    PERMISSION_DENIED: STOP,           # task-specific; switching providers won't help
    CLI_UNAVAILABLE: NEXT_PROFILE,
    TIMEOUT: NEXT_PROFILE,             # transient (API_TIMEOUT_MS): retry then next profile
    CLI_EXIT_ERROR: STOP,
    MISSING_REPORT: STOP,
}


def fallback_action(reason: str) -> str:
    return _ACTION.get(reason, STOP)


# Transient failures worth retrying within the SAME profile (a fresh process may
# succeed) before moving to the next. Both rate-limit and API timeout are transient:
# the live example shows claude itself retrying on API_TIMEOUT_MS up to 10 times.
RETRYABLE = (RATE_LIMITED, TIMEOUT)

# Failure reasons that mean "this provider/account can't serve us right now" —
# the only ones that escalate advisor/supervisor to executor_stuck when the whole
# chain is exhausted. A bare TIMEOUT is transient, NOT quota: if every profile times
# out we hand the last output back so the caller's existing NONE->turn_error path
# (consec_err budget) handles it, rather than hard-failing the goal.
QUOTA_FAMILY = (RATE_LIMITED, USAGE_EXHAUSTED, AUTH_ERROR, ACCOUNT_SUSPENDED, CLI_UNAVAILABLE)

# Reasons a text role (advisor/supervisor) acts on: quota family + transient timeout.
# Anything else (content error, clean exit) returns None -> caller parses as before.
_TEXT_ACTIONABLE = QUOTA_FAMILY + (TIMEOUT,)


def classify_text(stdout: str, exit_code: int, timed_out: bool) -> str | None:
    """Failure classification for runs with no report file (advisor/supervisor).

    Returns an actionable reason (quota family, or a transient TIMEOUT) so the chain
    can retry/fall back; else None — meaning 'let the caller's own parsing decide'
    (e.g. advisor NONE/MULTIPLE stays a content turn_error)."""
    text = protocol.strip_ansi(stdout or "")
    for reason, rx in _SIGNATURES:
        if rx.search(text) and reason in _TEXT_ACTIONABLE:
            return reason
    if timed_out:
        return TIMEOUT
    return None


def classify(stdout: str, exit_code: int, timed_out: bool,
             has_trailer: bool, file_changed: bool) -> str | None:
    """Return None if the run produced a valid report; else a failure reason.

    A valid report wins unconditionally (even done=false): if B wrote the trailer
    or produced/changed the report file, that's signal, not failure.
    """
    if has_trailer or file_changed:
        return None
    text = protocol.strip_ansi(stdout or "")
    # Signature scan first (see ordering note above).
    for reason, rx in _SIGNATURES:
        if rx.search(text):
            return reason
    if timed_out:
        return TIMEOUT
    if exit_code not in (0, None):
        return CLI_EXIT_ERROR
    # Ran to a clean exit but left no report.
    return MISSING_REPORT


# ---- report-file stale protection -------------------------------------------
def file_sig(path: str) -> tuple[bool, str | None]:
    """(exists, sha256) snapshot of a report file before execution. Used so a
    stale report left by a previous attempt is never mistaken for a fresh one."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, OSError):
        return (False, None)
    return (True, hashlib.sha256(data).hexdigest())


def file_changed(before: tuple[bool, str | None], path: str) -> bool:
    """True if the report file is newly created or its hash differs from `before`."""
    existed, before_sha = before
    after_existed, after_sha = file_sig(path)
    if not after_existed:
        return False
    if not existed:
        return True               # newly created
    return after_sha != before_sha


# ---- profile chain -----------------------------------------------------------
@dataclass
class Attempt:
    provider: str
    model: str | None
    reason: str | None            # None == produced report; else failure reason
    attempt: int                  # 1-based attempt number within the whole chain
    raw_excerpt: str = ""         # cleaned, truncated CLI output — so a misclassify is debuggable


@dataclass
class ExecOutcome:
    kind: str                     # "report" | "failed"
    report: object = None         # protocol.Report when kind == "report"
    res: object = None            # backends.RunResult of the accepted/last run
    provider: str | None = None
    model: str | None = None
    reason: str | None = None     # terminal failure reason when kind == "failed"
    attempts: list = field(default_factory=list)   # list[Attempt]
    exhausted: bool = False       # True when the whole chain was tried (vs. fail-fast STOP)


# OpenCode keeps state in a SQLite db with WAL; concurrent invocations across
# projects can corrupt it. Serialize every opencode spawn process-wide.
_opencode_lock = threading.Lock()

_CURSOR_ARGV_PROMPT = (
    "Read and execute the complete task from PROMPT_PATH: {abs}.\n"
    "Write the required report and finish with the required REPORT trailer.")


@dataclass
class Profile:
    provider: str
    model: str | None
    cmd: list
    max_attempts: int = 1         # retry budget for rate_limited within this profile
    input: str = "stdin"          # "stdin" | "argv_prompt" (cursor)
    # ADR-14 warm session (text roles only): when warm and a session_id exists, the
    # caller spawns resume_cmd (with {session_id} substituted) instead of cmd, and
    # sends only a delta prompt. start_cmd is the first (cold-seed) invocation.
    warm: bool = False
    start_cmd: list | None = None      # first turn of a warm session (else falls back to cmd)
    resume_cmd: list | None = None     # subsequent turns; contains "{session_id}" token


def resolve_session_id(provider: str, stdout: str) -> str | None:
    """Extract a resumable session id from a completed run's stdout, per provider.

    codex --json emits thread_id in the JSONL stream. kiro/cursor do NOT print an id
    in headless mode (their ids come from a list-diff / create-chat side channel), so
    they return None here and the caller resolves the id another way (Tầng 4)."""
    if provider == "codex":
        from . import advisorchat
        p = advisorchat.parse_jsonl(stdout or "")
        # Only trust the id when the turn actually completed (else resume would fail).
        return p.get("thread_id") if p.get("completed") else None
    return None


_KIRO_SID = re.compile(r"Chat SessionId:\s*([0-9a-fA-F-]{36})")


def kiro_session_ids(backend, cwd) -> set:
    """Snapshot kiro session ids for `cwd` via `--list-sessions` (read-only; does NOT
    spawn an agent). Used by a before/after diff to learn the id of a session kiro
    just created — kiro prints no id in headless run output (Tầng 4)."""
    try:
        cp = CommandProfile(["kiro-cli", "chat", "--list-sessions"], "supervisor")
        res = backend.run_once(cp, "", cwd, 30, 60)
        text = protocol.strip_ansi(res.stdout or "")
        return set(_KIRO_SID.findall(text))
    except Exception:
        return set()


_PROVIDER_BY_BIN = {
    "kiro-cli": "kiro", "opencode": "opencode", "claude": "claude",
    "cursor-agent": "cursor", "agent": "cursor",
}


def _bin_name(arg0: str) -> str:
    base = os.path.basename(str(arg0)).lower()
    for suf in (".cmd", ".bat", ".exe"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base


def _infer_provider(cmd: list) -> str:
    return _PROVIDER_BY_BIN.get(_bin_name(cmd[0]), _bin_name(cmd[0]))


def _infer_model(cmd: list) -> str | None:
    for i, a in enumerate(cmd):
        if a == "--model" and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _arg_after(cmd: list, flag: str) -> str | None:
    for i, a in enumerate(cmd):
        if a == flag and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def primary_profile(role_cfg) -> Profile:
    cmd = list(role_cfg.cmd)
    return Profile(_infer_provider(cmd), _infer_model(cmd), cmd, 1, "stdin")


def build_chain(role_cfg) -> list[Profile]:
    """Primary (from role_cfg.cmd) + declared fallbacks (provider/model/cmd/...)."""
    chain = [primary_profile(role_cfg)]
    for fb in (getattr(role_cfg, "fallbacks", None) or []):
        cmd = list(fb["cmd"])
        chain.append(Profile(
            provider=fb.get("provider") or _infer_provider(cmd),
            model=fb.get("model") or _infer_model(cmd),
            cmd=cmd,
            max_attempts=int(fb.get("max_attempts", 1)),
            input=fb.get("input", "stdin"),
        ))
    return chain


def _claude_agent_available(agent: str | None) -> bool:
    """Preflight: a named claude agent must exist as ~/.claude/agents/**/<name>.md so a
    missing agent yields cli_unavailable (next profile) instead of silently running
    Claude's default agent.

    Agents are organized in category subdirs (analysis/, core/, sparc/, ...) as
    <name>.md files, so we recurse (rglob) and match the .md extension — a top-level
    non-recursive glob misses them and falsely reports cli_unavailable."""
    if not agent:
        return True
    from pathlib import Path
    d = Path.home() / ".claude" / "agents"
    try:
        return any(d.rglob(f"{agent}.md"))
    except OSError:
        return False


def _attempt_dicts(attempts: list) -> list:
    return [{"provider": a.provider, "model": a.model, "reason": a.reason,
             "attempt": a.attempt, "raw_excerpt": a.raw_excerpt} for a in attempts]


def _write_attempt_log(log_dir, role, n, prof, reason, stdout) -> None:
    """Persist the full raw CLI output of one attempt so a misclassification (like the
    claude-preflight bug) is debuggable after the fact. Best-effort; never raises."""
    if not log_dir:
        return
    try:
        from pathlib import Path
        d = Path(log_dir) / "attempts"
        d.mkdir(parents=True, exist_ok=True)
        prov = getattr(prof, "provider", "?")
        model = (getattr(prof, "model", None) or "-").replace("/", "_")
        f = d / f"{role}-{n:02d}-{prov}-{model}-{reason or 'ok'}.log"
        f.write_text(stdout or "", encoding="utf-8")
    except Exception:
        pass


def _live_log_path(log_dir, role, n) -> str | None:
    """LS-019: path of the streaming live-log for one attempt (data_dir/live/<role>-<n>.log)."""
    if not log_dir:
        return None
    try:
        from pathlib import Path
        d = Path(log_dir) / "live"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{role}-{n:02d}.log")
    except Exception:
        return None


def _run_once(backend, cp, stdin, cwd, idle_timeout, hard_timeout, stop_re, live_log):
    """Call backend.run_once, passing live_log only if the backend accepts it (real
    GenericCliBackend does; test fakes with the old signature don't). Never fails over
    a missing kwarg."""
    try:
        return backend.run_once(cp, stdin, cwd, idle_timeout, hard_timeout,
                                stop_re=stop_re, live_log=live_log)
    except TypeError:
        return backend.run_once(cp, stdin, cwd, idle_timeout, hard_timeout, stop_re=stop_re)


def _spawn_profile(backend, prof: Profile, role: str, stdin_input: str,
                   prompt_path: str | None, cursor_agent_path: str | None,
                   cwd, idle_timeout, hard_timeout, stop_re, live_log=None):
    cmd = list(prof.cmd)
    if prof.provider == "cursor" and cursor_agent_path:
        cmd[0] = cursor_agent_path
    if prof.input == "argv_prompt":
        cmd = cmd + [_CURSOR_ARGV_PROMPT.format(abs=prompt_path)]
        stdin = ""
    else:
        stdin = stdin_input
    cp = CommandProfile(cmd, role)
    if prof.provider == "opencode":
        with _opencode_lock:                       # SQLite WAL: never run two at once
            return _run_once(backend, cp, stdin, cwd, idle_timeout, hard_timeout, stop_re, live_log)
    return _run_once(backend, cp, stdin, cwd, idle_timeout, hard_timeout, stop_re, live_log)


def run_with_fallback(role, role_cfg, backend, *, stdin_input, prompt_path,
                      report_rel, report_abs, cwd, idle_timeout, hard_timeout,
                      cursor_agent_path=None, emit=None, stop_re=None, log_dir=None) -> ExecOutcome:
    """Run an executor role across its profile chain until a valid report or the
    chain is exhausted. Emits executor_retry / executor_fallback along the way."""
    emit = emit or (lambda *a, **k: None)
    chain = build_chain(role_cfg)
    skipped: set = set()
    attempts: list = []
    n = 0
    fb_count = 0
    last_reason = None
    last_res = None

    def _next_viable(idx: int) -> Profile | None:
        for j in range(idx + 1, len(chain)):
            if chain[j].provider not in skipped:
                return chain[j]
        return None

    for idx, prof in enumerate(chain):
        if prof.provider in skipped:
            continue
        # Claude named-agent preflight (no spawn if the agent file is missing).
        if prof.provider == "claude" and not _claude_agent_available(_arg_after(prof.cmd, "--agent")):
            n += 1
            attempts.append(Attempt(prof.provider, prof.model, CLI_UNAVAILABLE, n,
                                    raw_excerpt="(preflight: claude agent file not found)"))
            last_reason = CLI_UNAVAILABLE
            nxt = _next_viable(idx)
            if nxt is not None:
                fb_count += 1
                emit("executor_fallback", from_provider=prof.provider, from_model=prof.model,
                     to_provider=nxt.provider, to_model=nxt.model,
                     reason=CLI_UNAVAILABLE, attempt=fb_count)
            continue

        reason = None
        max_att = max(1, prof.max_attempts)
        for att in range(1, max_att + 1):
            n += 1
            before = file_sig(report_abs)
            res = _spawn_profile(backend, prof, role, stdin_input, prompt_path,
                                 cursor_agent_path, cwd, idle_timeout, hard_timeout, stop_re,
                                 live_log=_live_log_path(log_dir, role, n))
            last_res = res
            rep = protocol.parse_report(res.stdout)
            has_trailer = rep.path is not None
            changed = file_changed(before, report_abs)
            reason = classify(res.stdout, res.exit_code, res.timed_out, has_trailer, changed)
            _write_attempt_log(log_dir, role, n, prof, reason, res.stdout)
            excerpt = protocol.clean_excerpt(res.stdout)[:800]
            if reason is None:
                if not has_trailer:                # backstop: file written without trailer
                    rep = protocol.Report(report_rel, True, None,
                                          protocol.clean_excerpt(res.stdout)[:500], res.stdout)
                attempts.append(Attempt(prof.provider, prof.model, None, n))
                return ExecOutcome("report", report=rep, res=res, provider=prof.provider,
                                   model=prof.model, attempts=attempts)
            attempts.append(Attempt(prof.provider, prof.model, reason, n, raw_excerpt=excerpt))
            last_reason = reason
            if reason in RETRYABLE and att < max_att:
                emit("executor_retry", provider=prof.provider, model=prof.model,
                     reason=reason, attempt=att, raw_excerpt=excerpt)
                continue                            # retry same profile (transient)
            break

        action = fallback_action(reason)
        if action == STOP:
            return ExecOutcome("failed", res=last_res, provider=prof.provider,
                               model=prof.model, reason=reason, attempts=attempts,
                               exhausted=False)
        if action == SKIP_PROVIDER:
            skipped.add(prof.provider)
        nxt = _next_viable(idx)
        if nxt is not None:
            fb_count += 1
            emit("executor_fallback", from_provider=prof.provider, from_model=prof.model,
                 to_provider=nxt.provider, to_model=nxt.model, reason=reason, attempt=fb_count,
                 raw_excerpt=attempts[-1].raw_excerpt if attempts else None)

    return ExecOutcome("failed", res=last_res, reason=last_reason or "executor_stuck",
                       attempts=attempts, exhausted=True)


# ---- advisor / supervisor fallback (no report file) -------------------------
@dataclass
class TextOutcome:
    """Result of a text-producing role (advisor/supervisor) run through fallback.

    stdout/res are ALWAYS the last run's output — the caller parses it with its own
    logic (advisor directive / supervisor JSON). `quota_failed` is True only when the
    whole chain was exhausted by quota-family errors and no profile produced
    non-quota output; the caller may then treat it as a transport failure.
    """
    stdout: str
    res: object
    provider: str | None = None
    model: str | None = None
    quota_failed: bool = False
    reason: str | None = None
    attempts: list = field(default_factory=list)
    # ADR-14: session id of the warm primary (codex thread_id / kiro list-diff id).
    # Set ONLY when the producing profile is the warm primary; None after any fallback
    # (a fallback provider's context is not in the primary's session).
    session_ref: str | None = None
    warm_used: bool = False        # True when this turn resumed an existing session


def chain_from(primary_cmd: list, fallbacks: list) -> list[Profile]:
    """Build a profile chain from a bare argv (advisor_cmd/supervisor_cmd) plus a
    list of fallback dicts. Used for roles that have no config.Role object."""
    cmd = list(primary_cmd)
    chain = [Profile(_infer_provider(cmd), _infer_model(cmd), cmd, 1, "stdin")]
    for fb in (fallbacks or []):
        fcmd = list(fb["cmd"])
        chain.append(Profile(
            provider=fb.get("provider") or _infer_provider(fcmd),
            model=fb.get("model") or _infer_model(fcmd),
            cmd=fcmd, max_attempts=int(fb.get("max_attempts", 1)),
            input=fb.get("input", "stdin")))
    return chain


def _warm_spawn(backend, prof, role, stdin_input, cwd, idle_timeout, hard_timeout,
                session_id, warm_start_cmd, warm_resume_cmd, live_log=None):
    """Spawn the primary warm profile: resume_cmd (session exists) else start_cmd.
    Returns RunResult. No stop_re — warm needs full JSONL incl. turn.completed."""
    if session_id and warm_resume_cmd:
        cmd = [a.replace("{session_id}", session_id) for a in warm_resume_cmd]
    else:
        cmd = list(warm_start_cmd or prof.cmd)
    cp = CommandProfile(cmd, role)
    if prof.provider == "opencode":
        with _opencode_lock:
            return _run_once(backend, cp, stdin_input, cwd, idle_timeout, hard_timeout, None, live_log)
    return _run_once(backend, cp, stdin_input, cwd, idle_timeout, hard_timeout, None, live_log)


def run_text_with_fallback(role, primary_cmd, fallbacks, backend, *, stdin_input,
                           cwd, idle_timeout, hard_timeout, cursor_agent_path=None,
                           prompt_path=None, emit=None, stop_re=None, log_dir=None,
                           warm=False, session_id=None, warm_start_cmd=None,
                           warm_resume_cmd=None, warm_stdin=None) -> TextOutcome:
    """Run advisor/supervisor across [primary]+fallbacks, switching profile ONLY on
    actionable errors (quota family + transient timeout). Any other output (valid OR
    a content error like an advisor NONE directive) is returned as-is for the caller
    to parse — preserving existing harness semantics (turn_error/consec_err stay).

    ADR-14 warm: when `warm` and the FIRST (primary) profile runs, it uses the warm
    start/resume cmd (no stop_re, parse JSONL for thread_id) and `warm_stdin` (the
    delta prompt when session_id is set). On any fallback to a non-primary provider,
    warm is dropped — TextOutcome.provider tells the caller, which invalidates its
    session. The resolved session id (codex thread_id) is returned in .session_ref."""
    emit = emit or (lambda *a, **k: None)
    chain = chain_from(primary_cmd, fallbacks)
    skipped: set = set()
    attempts: list = []
    n = 0
    fb_count = 0
    last_res = None
    last_reason = None
    new_session_id = None

    def _next_viable(idx: int) -> Profile | None:
        for j in range(idx + 1, len(chain)):
            if chain[j].provider not in skipped:
                return chain[j]
        return None

    for idx, prof in enumerate(chain):
        if prof.provider in skipped:
            continue
        if prof.provider == "claude" and not _claude_agent_available(_arg_after(prof.cmd, "--agent")):
            n += 1
            attempts.append(Attempt(prof.provider, prof.model, CLI_UNAVAILABLE, n,
                                    raw_excerpt="(preflight: claude agent file not found)"))
            last_reason = CLI_UNAVAILABLE
            nxt = _next_viable(idx)
            if nxt is not None:
                fb_count += 1
                emit("executor_fallback", role=role, from_provider=prof.provider,
                     from_model=prof.model, to_provider=nxt.provider, to_model=nxt.model,
                     reason=CLI_UNAVAILABLE, attempt=fb_count)
            continue

        # Warm applies ONLY to the primary profile (idx 0); fallbacks are always cold.
        is_warm = warm and idx == 0
        reason = None
        max_att = max(1, prof.max_attempts)
        for att in range(1, max_att + 1):
            n += 1
            live_log = _live_log_path(log_dir, role, n)
            if is_warm:
                # delta prompt when resuming an existing session, else the full seed
                this_stdin = (warm_stdin if (session_id and warm_stdin is not None)
                              else stdin_input)
                res = _warm_spawn(backend, prof, role, this_stdin, cwd, idle_timeout,
                                  hard_timeout, session_id, warm_start_cmd, warm_resume_cmd,
                                  live_log=live_log)
            else:
                res = _spawn_profile(backend, prof, role, stdin_input, prompt_path,
                                     cursor_agent_path, cwd, idle_timeout, hard_timeout, stop_re,
                                     live_log=live_log)
            last_res = res
            reason = classify_text(res.stdout, res.exit_code, res.timed_out)
            _write_attempt_log(log_dir, role, n, prof, reason, res.stdout)
            excerpt = protocol.clean_excerpt(res.stdout)[:800]
            if reason is None:
                attempts.append(Attempt(prof.provider, prof.model, None, n))
                out_stdout = res.stdout
                if is_warm and prof.provider == "codex":
                    # codex --json wraps the directive in JSONL (newlines escaped).
                    # Resolve the session id + unwrap the reply (real newlines) so the
                    # caller's protocol.parse sees the directive, not the raw stream.
                    from . import advisorchat
                    p = advisorchat.parse_jsonl(res.stdout or "")
                    new_session_id = p.get("thread_id") if p.get("completed") else None
                    if p.get("reply"):
                        out_stdout = p["reply"]
                        res = RunResult(out_stdout, res.exit_code, res.timed_out, res.session_ref)
                elif is_warm:
                    new_session_id = resolve_session_id(prof.provider, res.stdout)
                return TextOutcome(out_stdout, res, prof.provider, prof.model,
                                   quota_failed=False, attempts=attempts,
                                   session_ref=new_session_id)
            attempts.append(Attempt(prof.provider, prof.model, reason, n, raw_excerpt=excerpt))
            last_reason = reason
            if reason in RETRYABLE and att < max_att:
                emit("executor_retry", role=role, provider=prof.provider,
                     model=prof.model, reason=reason, attempt=att, raw_excerpt=excerpt)
                continue
            break

        action = fallback_action(reason)
        if action == STOP:          # actionable reasons never map to STOP, but be safe
            return TextOutcome(last_res.stdout if last_res else "", last_res,
                               prof.provider, prof.model, quota_failed=True,
                               reason=reason, attempts=attempts)
        if action == SKIP_PROVIDER:
            skipped.add(prof.provider)
        nxt = _next_viable(idx)
        if nxt is not None:
            fb_count += 1
            emit("executor_fallback", role=role, from_provider=prof.provider,
                 from_model=prof.model, to_provider=nxt.provider, to_model=nxt.model,
                 reason=reason, attempt=fb_count,
                 raw_excerpt=attempts[-1].raw_excerpt if attempts else None)

    # Chain exhausted with no usable output. If the only failures were transient
    # timeouts (no quota signature), DON'T hard-fail: hand the last output back so
    # the caller's NONE->turn_error/consec_err budget governs. Hard-fail only when a
    # real quota-family error was seen.
    quota_seen = any(a.reason in QUOTA_FAMILY for a in attempts)
    return TextOutcome(last_res.stdout if last_res else "", last_res,
                       quota_failed=quota_seen, reason=last_reason or "quota_exhausted",
                       attempts=attempts)
