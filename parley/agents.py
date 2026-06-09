"""DEPRECATED (ADR-10): superseded by parley.backends. Không được harness/cli import nữa; giữ để tham khảo.

Parley agent adapters (ADR-06 harness owns spawn). Prompt via STDIN; idle/hard timeout + kill.

A = codex (read-only). B = kiro role. verify = multi-gate (R2-4), PASS = all exit 0.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class Verify:
    code: int
    failed_gate: str | None
    tail: str


def _run(cmd, stdin_text, idle_timeout, hard_timeout, cwd=None) -> str:
    """Spawn cmd, feed stdin, stream stdout; kill on idle>idle_timeout or total>hard_timeout."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd)
    buf, last = [], [time.time()]

    def reader():
        assert p.stdout is not None
        for line in p.stdout:
            buf.append(line)
            last[0] = time.time()

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        if p.stdin:
            p.stdin.write(stdin_text)
            p.stdin.close()
    except Exception:
        pass
    start = time.time()
    while p.poll() is None:
        now = time.time()
        if now - last[0] > idle_timeout or now - start > hard_timeout:
            p.kill()
            break
        time.sleep(0.2)
    th.join(timeout=2)
    return "".join(buf)


class Agents:
    def __init__(self, cfg):
        self.cfg = cfg

    def run_advisor(self, ctx: str, nonce: str) -> str:
        prompt = ctx.replace("{N}", nonce)
        return _run(self.cfg.advisor_cmd, prompt, self.cfg.limits.idle_timeout_s,
                    self.cfg.limits.hard_timeout_s, cwd=self.cfg.project_dir)

    def run_role(self, role: str, prompt: str) -> str:
        return _run(self.cfg.roles[role].cmd, prompt, self.cfg.limits.idle_timeout_s,
                    self.cfg.limits.hard_timeout_s, cwd=self.cfg.project_dir)

    def run_verify(self) -> Verify:
        last = ""
        for gate in self.cfg.verify.gates:
            r = subprocess.run(gate, cwd=self.cfg.project_dir, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=self.cfg.verify.timeout_s)
            last = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return Verify(r.returncode, " ".join(gate), last[-4000:])
        return Verify(0, None, last[-4000:])
