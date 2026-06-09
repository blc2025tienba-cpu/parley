"""Parley harness: deterministic loop (mục 7.2) + guardrails + fsm gate. The heart.

Dependencies (channel, agents, supervisor-runner, git-commit) are injected so the
loop is unit-testable with fakes (no real CLI).
"""
from __future__ import annotations

import os
import re
import secrets

from . import context, executor, gitio, housekeeping, protocol, supervisor
from .backends import CommandProfile
from .fsm import Fsm

_REPORT_STOP = re.compile(r'<<<REPORT\b[^>\n]*>>>')


def new_nonce() -> str:
    return secrets.token_hex(3)


def new_task_id() -> str:
    return "task_" + secrets.token_hex(4)


def _abs(base, path):
    return path if (path and os.path.isabs(path)) else os.path.join(base, path or "")


class Harness:
    def __init__(self, cfg, channel, backend, verify_runner, sup_runner,
                 git_commit=gitio.commit_slice, git_push=gitio.push):
        self.cfg = cfg
        self.ch = channel
        self.backend = backend
        self.verify_runner = verify_runner
        self.sup_runner = sup_runner
        self.git_commit = git_commit
        self.git_push = git_push
        self.fsm = Fsm()
        self.state = "running"
        self.stop_reason = None
        self.turn = 0
        self._last_sig = None
        self.progress = []
        self.recon = None
        self.awaiting_report_review = False
        self.review_slice = None        # slice của report đang chờ advisor review (ADR-11)
        self.review_edit = False
        self.review_task_id = None      # task của report đang chờ review (S1)
        self.pending_parent = None      # parent cho task emergent kế (sau REJECT)
        self.slice_task = {}            # slice -> task_id hiện tại
        self.advisor_session = None     # ADR-14: codex thread_id warm trong phase (None = cold)
        self._last_diff = ""
        self._last_excerpt = ""
        self.advisor_profile = CommandProfile(cfg.advisor_cmd, "advisor")

    def stop(self, reason):
        self.state = "stopped"
        self.stop_reason = reason

    def _maybe_commit(self, sl):
        if self.fsm.slice_done(sl):
            msg = housekeeping.suggest_commit_message(
                self.cfg, self.backend, sl, self._last_diff, f"parley: slice {sl}")
            sha = self.git_commit(self.cfg.project_dir, sl, msg)
            self.ch.emit("slice_done", slice=sl, commit=sha, task_id=self.slice_task.get(sl))
            self.progress.append(f"- slice {sl} DONE (commit {(sha or '')[:7]})")
            if housekeeping.update_changelog(self.cfg, self.backend, sl, self._last_excerpt):
                self.ch.emit("housekeeping", slice=sl, doc=self.cfg.housekeeping.changelog_path)

    def _merge_control(self, dec, ctl):
        """Supervisor wins conflicts (rule 8); observer stop/steer otherwise applies."""
        if not ctl:
            return dec
        cv = ctl.get("verdict")
        if dec.verdict == "stop":
            if cv and cv not in ("stop", None):
                self.ch.log_decision(actor="harness", event="conflict",
                                     winner="supervisor", observer_seq=ctl.get("seq"))
            return dec
        if cv == "stop":
            self.ch.log_decision(actor="harness", event="conflict",
                                 winner="observer", observer_seq=ctl.get("seq"))
            return supervisor.Decision("stop", "observer stop")
        if cv == "steer" and dec.verdict != "steer":
            return supervisor.Decision("steer", "observer steer", ctl.get("inject", ""))
        return dec

    def run(self, goal, contract, first_phase):
        cfg, ch = self.cfg, self.ch
        st = ch.resume()
        if st:
            self.turn = st.turn
            if st.pending_dispatch:
                self.state = "paused"
                ch.emit("resume", note="dispatch chua co report -> cho nguoi")
                return "paused"
        else:
            ch.emit("user_goal", goal=goal, contract_path=cfg.contract_path)
        phase = first_phase
        ch.emit("phase_start", phase=phase)
        a_ctx = context.advisor_seed(goal, contract, phase)
        a_delta = None                   # ADR-14: warm-turn delta (None = send full a_ctx)
        self.advisor_session = None
        warm_turns = 0                   # ADR-14: consecutive warm turns in this phase
        iters = consec_err = 0
        while self.state == "running" and self.turn < cfg.limits.max_turns:
            iters += 1
            if iters > cfg.limits.hard_iter_cap:
                self.stop("stuck")
                break
            ctl = ch.read_control()                                  # R2-8: top of loop
            if ctl and ctl.get("verdict") == "stop":
                self.stop("observer")
                break
            if ctl and ctl.get("verdict") == "pause":
                ch.emit("paused")
                self.state = "paused"
                break
            nonce = new_nonce()
            stop_re = re.compile(r'<<<(?:END|COMPLETE|VERIFY|PHASE)\b[^>\n]*nonce="'
                                 + re.escape(nonce) + r'"')
            # ADR-14: warm advisor session per-phase. When a session is live, send only
            # the delta (a_delta) — the session remembers goal/contract/policy. Cold
            # (no session yet) sends the full a_ctx seed. ADR-15: advisor still falls
            # back on quota-family errors; a fallback to a non-primary provider drops
            # the warm session (handled after the call).
            warm = bool(cfg.advisor_warm)
            # ADR-14 force-cold: after N warm turns in a phase, drop the session and
            # re-seed with FULL context (a_ctx), never a delta — a fresh session must
            # carry goal/contract/policy. Also re-seeds if a_delta isn't available yet.
            if warm and warm_turns >= cfg.limits.max_warm_turns_per_phase:
                self.advisor_session = None
                warm_turns = 0
            warm_stdin = (a_delta.replace("{N}", nonce)
                          if (warm and self.advisor_session and a_delta is not None) else None)
            adv = executor.run_text_with_fallback(
                "advisor", cfg.advisor_cmd, cfg.advisor_fallbacks, self.backend,
                stdin_input=a_ctx.replace("{N}", nonce), cwd=cfg.project_dir,
                idle_timeout=cfg.limits.idle_timeout_s, hard_timeout=cfg.limits.hard_timeout_s,
                cursor_agent_path=cfg.cursor_agent_path,
                emit=lambda et, **kw: ch.emit(et, phase=phase, turn=self.turn, **kw),
                stop_re=(None if warm else stop_re), log_dir=cfg.data_dir,
                warm=warm, session_id=self.advisor_session,
                warm_start_cmd=cfg.advisor_warm_start_cmd,
                warm_resume_cmd=cfg.advisor_warm_resume_cmd, warm_stdin=warm_stdin)
            res = adv.res
            if adv.quota_failed:
                ch.emit("executor_exhausted", role="advisor", reason=adv.reason,
                        attempts=executor._attempt_dicts(adv.attempts))
                self.stop("executor_stuck")
                break
            # Maintain the warm session: keep it only while the primary (codex) served
            # this turn and yielded a resumable id; any fallback or missing id => cold.
            if warm and adv.provider == "codex" and adv.session_ref:
                self.advisor_session = adv.session_ref
                warm_turns += 1
            else:
                self.advisor_session = None
                warm_turns = 0
            d = protocol.parse(res.stdout, nonce)
            if d.kind in ("NONE", "MULTIPLE"):
                consec_err += 1
                ch.emit("turn_error", reason=("advisor_timeout" if res.timed_out else d.kind),
                        raw_excerpt=protocol.strip_ansi(res.stdout)[:800])
                if consec_err > cfg.limits.max_turn_errors:
                    self.stop("stuck")
                    break
                a_ctx = context.advisor_reject(goal, contract, phase, self.progress, d.kind, self.recon)
                a_delta = context.advisor_reject_delta(self.progress, d.kind)
                continue
            if self.awaiting_report_review and d.review not in ("APPROVE", "REJECT"):
                consec_err += 1
                ch.emit("turn_error", reason="advisor_missing_report_review",
                        raw_excerpt=protocol.strip_ansi(res.stdout)[:800])
                if consec_err > cfg.limits.max_turn_errors:
                    self.stop("stuck")
                    break
                continue
            consec_err = 0
            ch.emit("advisor", phase=phase, turn=self.turn, verdict=d.review,
                    action=d.kind, role=d.role, slice=d.slice, prompt=d.prompt,
                    reviews_task=self.review_task_id)
            self.awaiting_report_review = False
            # ADR-11 hybrid: advisor review của report trước = cổng commit
            if self.review_slice and d.review in ("APPROVE", "REJECT"):
                self.fsm.observe({"slice": self.review_slice, "verdict": d.review})
                if d.review == "APPROVE" and not self.review_edit:
                    self.fsm.observe_verify(self.review_slice, 0)   # read-only: no code -> verify vacuous
                if d.review == "REJECT":
                    self.pending_parent = self.review_task_id       # S1: fixer kế = emergent child
                self._maybe_commit(self.review_slice)
                self.review_slice = None
            if d.kind == "COMPLETE":
                self.stop("done")
                break
            if d.kind == "VERIFY":
                v = self.verify_runner.run(cfg.verify.gates, cfg.project_dir, cfg.verify.timeout_s)
                ch.emit("verify", slice=d.slice, exit=v.code, failed_gate=v.failed_gate, tail=v.tail)
                self.fsm.observe_verify(d.slice, v.code)
                self.progress.append(f"- verify slice={d.slice} -> exit={v.code}")
                self._maybe_commit(d.slice)
                a_ctx = context.advisor_verify(goal, contract, phase, self.progress,
                                               {"exit": v.code, "failed_gate": v.failed_gate, "tail": v.tail},
                                               self.recon)
                a_delta = context.advisor_verify_delta(self.progress,
                                                       {"exit": v.code, "failed_gate": v.failed_gate, "tail": v.tail})
                continue
            if d.kind == "PHASE":
                ch.emit("phase_end", reconciliation_path=d.reconciliation)
                phase = d.phase
                self.recon = d.reconciliation
                self.progress = []
                ch.emit("phase_start", phase=phase)
                a_ctx = context.advisor_seed(goal, contract, phase, prev_reconciliation=d.reconciliation)
                a_delta = None
                self.advisor_session = None      # ADR-14: new phase -> force cold re-seed
                warm_turns = 0
                if hasattr(self.sup_runner, "reset"):
                    self.sup_runner.reset()      # ADR-14: supervisor session resets per phase
                continue
            # DISPATCH
            sig = (d.role, d.slice, d.prompt)
            if sig == self._last_sig:
                self.stop("loop")
                break
            self._last_sig = sig
            if not self.fsm.allow(d, mode=cfg.pipeline_mode):
                ch.log_decision(actor="harness", event="deviation", note=self.fsm.why)
                if cfg.pipeline_mode == "strict":
                    a_ctx = context.advisor_reject(goal, contract, phase, self.progress, self.fsm.why, self.recon)
                    a_delta = context.advisor_reject_delta(self.progress, self.fsm.why)
                    continue
            gate_due = (self.turn % cfg.every_n_turns == 0) or cfg.roles[d.role].edit   # R2-11
            dec = (supervisor.gate(goal, contract, d, ch.tail(), self.sup_runner)
                   if gate_due else supervisor.CONTINUE)
            dec = self._merge_control(dec, ctl)
            if dec.verdict == "stop":
                self.stop("gov")
                break
            if dec.verdict == "steer":
                d.prompt = (d.prompt or "") + "\n\n[STEER] " + dec.inject
            task_id = new_task_id()
            origin = "emergent" if self.pending_parent else "planned"
            parent_task_id = self.pending_parent
            self.pending_parent = None
            self.slice_task[d.slice] = task_id
            report_path = f"docs/reports/phase{phase}-slice-{d.slice}-{d.role}-report.md"
            prompt_snap = ch.write_prompt(task_id, context.role_prompt_document(
                d, contract, cfg.project_dir, cfg.contract_path, report_path, task_id),
                project_dir=cfg.project_dir)
            ch.emit("gov", verdict=dec.verdict, reason=dec.reason)
            ch.emit("dispatch", phase=phase, turn=self.turn, role=d.role, slice=d.slice,
                    prompt=d.prompt, proposed_by="advisor", authorized_by="supervisor",
                    task_id=task_id, origin=origin, parent_task_id=parent_task_id,
                    prompt_path=prompt_snap.path, prompt_sha256=prompt_snap.sha,
                    title=(d.prompt or "").strip().split("\n")[0][:80])
            abs_rp = _abs(cfg.project_dir, report_path)

            def _emit_exec(etype, **kw):
                ch.emit(etype, phase=phase, turn=self.turn, role=d.role, slice=d.slice,
                        task_id=task_id, **kw)

            outcome = executor.run_with_fallback(
                d.role, cfg.roles[d.role], self.backend,
                stdin_input=context.executor_input(
                    d, contract, cfg.project_dir, cfg.contract_path, report_path,
                    prompt_path=prompt_snap.path),
                prompt_path=prompt_snap.path, report_rel=report_path, report_abs=abs_rp,
                cwd=cfg.project_dir, idle_timeout=cfg.limits.idle_timeout_s,
                hard_timeout=cfg.limits.hard_timeout_s,
                cursor_agent_path=cfg.cursor_agent_path, emit=_emit_exec, stop_re=_REPORT_STOP,
                log_dir=str(ch.dir))

            # ADR-15: executor that could not produce a report does NOT go to the
            # Advisor as done=false (that was the REJECT/quota-burn loop). Stop clean.
            if outcome.kind == "failed":
                _emit_exec("executor_exhausted" if outcome.exhausted else "executor_error",
                           reason=outcome.reason, provider=outcome.provider,
                           model=outcome.model, attempts=executor._attempt_dicts(outcome.attempts))
                self.stop("executor_stuck")
                break

            rep, res = outcome.report, outcome.res
            snap = None
            if rep.path:
                try:
                    snap = self.ch.snapshot_report(_abs(cfg.project_dir, rep.path))
                except Exception:
                    snap = None
            ch.emit("report", phase=phase, turn=self.turn, role=d.role, slice=d.slice,
                    report_path=rep.path, snapshot_path=(snap.path if snap else None),
                    sha256=(snap.sha if snap else None), excerpt=rep.excerpt,
                    done=rep.done, verdict=rep.verdict, session_ref=res.session_ref,
                    task_id=task_id, provider=outcome.provider, model=outcome.model,
                    attempts=executor._attempt_dicts(outcome.attempts))
            self.fsm.observe({"slice": d.slice, "verdict": rep.verdict})
            self._maybe_commit(d.slice)
            self.progress.append(f"- dispatch {d.role} slice={d.slice} -> done={rep.done} verdict={rep.verdict}")
            self.awaiting_report_review = True
            self.review_slice = d.slice
            self.review_edit = cfg.roles[d.role].edit
            self.review_task_id = task_id
            diff = gitio.diff(cfg.project_dir) if cfg.roles[d.role].edit else ""   # ADR-11: feed real diff to A
            self._last_diff = diff
            self._last_excerpt = rep.excerpt or ""
            a_ctx = context.advisor_followup(goal, contract, phase, self.progress,
                                             {"slice": d.slice, "role": d.role, "report_path": rep.path,
                                              "excerpt": rep.excerpt, "done": rep.done,
                                              "verdict": rep.verdict, "diff": diff},
                                             self.recon)
            a_delta = context.advisor_followup_delta(self.progress,
                                                     {"slice": d.slice, "role": d.role, "report_path": rep.path,
                                                      "excerpt": rep.excerpt, "done": rep.done,
                                                      "verdict": rep.verdict, "diff": diff})
            self.turn += 1
        # ADR-11: push CHỈ khi goal hoàn thành thành công, opt-in, work branch only
        if self.stop_reason == "done" and cfg.git.auto_push and cfg.git.push_on == "complete":
            ok = self.git_push(cfg.project_dir, cfg.git.branch, cfg.git.remote)
            ch.emit("push", branch=cfg.git.branch, ok=bool(ok))
        ch.emit("stopped", reason=self.stop_reason or self.state)
        return self.stop_reason
