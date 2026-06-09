"""Parley backends + verify runner (ADR-10).

Harness depends ONLY on AgentBackend.run_once -> RunResult (and SessionBackend.run_turn)
plus VerifyRunner.run -> Verify. Transport/session semantics never leak into the harness.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CommandProfile:
    cmd: list           # argv prefix; prompt luôn qua STDIN, KHÔNG qua argv
    name: str = ""      # advisor | <role> | supervisor — backend KHÔNG suy luận governance từ đây


@dataclass
class RunResult:
    stdout: str
    exit_code: int
    timed_out: bool = False
    session_ref: str | None = None   # chỉ phục vụ audit/resume; KHÔNG ảnh hưởng FSM


@dataclass
class Verify:
    code: int
    failed_gate: str | None
    tail: str


class AgentBackend(Protocol):
    def run_once(self, profile: CommandProfile, prompt: str, cwd: str | None,
                 idle_timeout: int, hard_timeout: int, stop_re=None) -> RunResult: ...


class VerifyRunner(Protocol):
    def run(self, gates: list, cwd: str | None, timeout: int) -> Verify: ...


class SessionBackend(AgentBackend, Protocol):
    """Opt-in (ADR-10). Phơi run_turn() cấp cao; send()/wait() KHÔNG được rò vào harness.

    Concrete impls (HeadlessResumeBackend / InteractiveAttachBackend) để dành — xem ADR-10.
    Attach mode KHÔNG được tuyên bố có deterministic kill / complete audit / reliable resume.
    """
    def run_turn(self, profile: CommandProfile, prompt: str, cwd: str | None,
                 idle_timeout: int, hard_timeout: int) -> RunResult: ...

    def detect_sessions(self) -> list: ...

    def stop(self, session_ref: str) -> None: ...


def _resolve(cmd: list) -> list:
    """Resolve argv[0] on PATH (honors Windows PATHEXT) and run .cmd/.bat via cmd /c."""
    exe = shutil.which(cmd[0]) or cmd[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def _spawn(cmd, stdin_text, idle_timeout, hard_timeout, cwd=None, stop_re=None,
           live_log=None) -> RunResult:
    """Spawn cmd, feed stdin, stream stdout.

    Stops (kills) when: a line matches stop_re (normal completion — process needn't exit),
    or idle>idle_timeout / total>hard_timeout (timed_out=True).

    LS-019: when `live_log` (a path) is given, every stdout line is appended+flushed there
    in real time, prefixed with a header carrying the OS pid. A watcher (notify/UI) can tail
    this file to see the last line + mtime and tell "working" from "hung" — the in-memory buf
    only materializes after the process ends (useless for a 0-byte timeout)."""
    p = subprocess.Popen(_resolve(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd)
    buf, last = [], [time.time()]
    stop_hit = threading.Event()
    lf = None
    if live_log:
        try:
            lf = open(live_log, "w", encoding="utf-8", newline="\n")
            lf.write(f"# pid={p.pid} start={time.strftime('%Y-%m-%dT%H:%M:%S')} "
                     f"cmd={' '.join(str(c) for c in cmd[:3])}\n")
            lf.flush()
        except Exception:
            lf = None

    def reader():
        assert p.stdout is not None
        for line in p.stdout:
            buf.append(line)
            last[0] = time.time()
            if lf is not None:
                try:
                    lf.write(line)
                    lf.flush()
                except Exception:
                    pass
            if stop_re is not None and stop_re.search(line):
                stop_hit.set()
                break

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        if p.stdin:
            p.stdin.write(stdin_text)
            p.stdin.close()
    except Exception:
        pass
    start, timed_out = time.time(), False
    while p.poll() is None:
        if stop_hit.is_set():
            p.kill()
            break
        now = time.time()
        if now - last[0] > idle_timeout or now - start > hard_timeout:
            p.kill()
            timed_out = True
            break
        time.sleep(0.1)
    th.join(timeout=2)
    try:
        p.wait(timeout=2)
    except Exception:
        pass
    try:
        if p.stdout and not p.stdout.closed:
            p.stdout.close()
    except Exception:
        pass
    if lf is not None:
        try:
            lf.write(f"# end exit={p.poll()} timed_out={timed_out} "
                     f"at={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
            lf.close()
        except Exception:
            pass
    code = p.poll()
    return RunResult("".join(buf), code if code is not None else -1, timed_out)


class GenericCliBackend:
    """stateless stdin/stdout one-shot — phù hợp codex exec + kiro-cli --no-interactive."""

    def run_once(self, profile: CommandProfile, prompt: str, cwd: str | None,
                 idle_timeout: int, hard_timeout: int, stop_re=None, live_log=None) -> RunResult:
        return _spawn(profile.cmd, prompt, idle_timeout, hard_timeout, cwd=cwd,
                      stop_re=stop_re, live_log=live_log)


class SubprocessVerifyRunner:
    """Chạy các verify gate tuần tự; PASS = tất cả exit 0 (R2-4). Độc lập backend."""

    def run(self, gates: list, cwd: str | None, timeout: int) -> Verify:
        last = ""
        for gate in gates:
            r = subprocess.run(_resolve(gate), cwd=cwd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
            last = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return Verify(r.returncode, " ".join(gate), last[-4000:])
        return Verify(0, None, last[-4000:])
