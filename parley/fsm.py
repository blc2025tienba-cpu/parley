"""Parley FSM verdict-gate (ADR-03, R2-1/R2-9).

Per-slice state: approve, verify_ok, last_verdict, committed.
allow() enforces strict invariants; slice_done() fires exactly once -> commit.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _S:
    approve: bool = False
    verify_ok: bool = False
    last_verdict: str | None = None
    committed: bool = False


class Fsm:
    def __init__(self):
        self.slices: dict[str, _S] = {}
        self.current: str | None = None
        self.why: str = ""

    def _s(self, sl: str) -> _S:
        return self.slices.setdefault(sl, _S())

    def observe(self, report) -> None:
        sl = report.get("slice") if isinstance(report, dict) else report.slice
        v = report.get("verdict") if isinstance(report, dict) else report.verdict
        if not sl:
            return
        s = self._s(sl)
        if v == "APPROVE":
            s.approve, s.last_verdict = True, "APPROVE"
        elif v == "REJECT":
            s.approve, s.last_verdict = False, "REJECT"

    def observe_verify(self, sl: str, exit_code: int) -> None:
        if sl:
            self._s(sl).verify_ok = (exit_code == 0)

    def slice_done(self, sl: str) -> bool:
        s = self._s(sl)
        if s.approve and s.verify_ok and not s.committed:
            s.committed = True
            return True
        return False

    def allow(self, d, mode=None) -> bool:
        """Gate a DISPATCH. Sets self.why on violation. (Caller decides enforce by mode.)"""
        sl, role = d.slice, d.role
        self.why = ""
        if not sl:
            self.why = "dispatch thieu slice"
            return False
        if self.current is not None and sl != self.current:
            cur = self._s(self.current)
            if not (cur.approve and cur.verify_ok):
                self.why = f"chuyen slice {self.current}->{sl} khi chua APPROVE+verify_ok"
                return False
        if sl == self.current and self._s(sl).last_verdict == "REJECT" and role != "fixer":
            self.why = f"slice {sl} REJECT -> phai dispatch fixer (nhan {role})"
            return False
        self.current = sl
        return True
