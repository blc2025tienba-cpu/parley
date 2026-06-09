"""Parley task projection (S1, ADR-13).

Task-list = VIEW dựng từ conversation.ndjson (nguồn sự thật duy nhất), KHÔNG store riêng.
Mỗi `dispatch` = một task instance (task_id, origin, parent_task_id, title); status suy ra từ
các event sau (report/advisor/verify/slice_done). Tolerant với log cũ thiếu task_id (fallback theo slice).
"""
from __future__ import annotations


def project_tasks(events: list) -> list:
    tasks: dict = {}
    order: list = []
    by_slice: dict = {}

    def _match(ev):
        tid = ev.get("task_id")
        if tid and tid in tasks:
            return tid
        return by_slice.get(ev.get("slice"))

    for e in events:
        t = e.get("type")
        if t == "dispatch":
            tid = e.get("task_id") or f"{e.get('slice')}@t{e.get('turn')}"
            tasks[tid] = {
                "id": tid,
                "title": e.get("title") or f"{e.get('role')}: {e.get('slice')}",
                "role": e.get("role"), "slice": e.get("slice"),
                "origin": e.get("origin", "planned"),
                "parent_task_id": e.get("parent_task_id"),
                "turn": e.get("turn"), "status": "running",
                "done": False, "verdict": None, "commit": None,
                # ADR-15: which CLI/model is running this task + fallback trail.
                "provider": None, "model": None, "fallback_count": 0, "attempts": [],
            }
            order.append(tid)
            by_slice[e.get("slice")] = tid
        elif t in ("executor_retry", "executor_fallback"):
            tid = _match(e)
            if tid:
                tk = tasks[tid]
                if t == "executor_fallback":
                    tk["fallback_count"] = tk.get("fallback_count", 0) + 1
                    tk["provider"] = e.get("to_provider")
                    tk["model"] = e.get("to_model")
        elif t in ("executor_error", "executor_exhausted"):
            tid = _match(e)
            if tid:
                tasks[tid].update(status="executor_stuck", reason=e.get("reason"),
                                  attempts=e.get("attempts") or tasks[tid].get("attempts", []))
        elif t == "report":
            tid = _match(e)
            if tid:
                upd = {"status": "awaiting_advisor_review",
                       "done": bool(e.get("done")), "verdict": e.get("verdict")}
                # report carries the provider/model that actually produced it + attempts trail.
                if e.get("provider"):
                    upd["provider"] = e.get("provider")
                if e.get("model"):
                    upd["model"] = e.get("model")
                if e.get("attempts"):
                    upd["attempts"] = e.get("attempts")
                tasks[tid].update(**upd)
        elif t == "advisor":
            tid = e.get("reviews_task")
            if tid in tasks:
                v = e.get("verdict")
                if v == "REJECT":
                    tasks[tid]["status"] = "rejected"
                elif v == "APPROVE" and tasks[tid]["status"] != "done":
                    tasks[tid]["status"] = "verifying"
        elif t == "verify":
            tid = by_slice.get(e.get("slice"))
            if tid and tasks[tid]["status"] not in ("done", "rejected"):
                tasks[tid]["status"] = "verifying"
        elif t == "slice_done":
            tid = e.get("task_id") or by_slice.get(e.get("slice"))
            if tid in tasks:
                tasks[tid].update(status="done", commit=e.get("commit"))

    return [tasks[i] for i in order]
