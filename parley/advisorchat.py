"""Parley advisor chat (warm-ish via re-seed; read-only). B1.

codex exec resume KHÔNG giữ được sandbox=read-only → ta re-seed lịch sử mỗi lượt với
`codex exec --sandbox read-only -o <tmp>` (advisor luôn read-only). build_prompt thuần (test được).
"""
from __future__ import annotations

import json

_SYS = (
    "Ban la ADVISOR cua project nay (orchestrator, READ-ONLY: khong sua code/chay tool). "
    "Tro chuyen tu nhien voi user: gioi thieu, ban bac, tu van huong di. "
    "Khi user yeu cau CHOT KE HOACH, de xuat goal-list ro rang. "
    "Neu user muon tao/cap nhat tai lieu, hay soan noi dung Markdown hoan chinh trong reply; "
    "end-user se duyet va luu bang Artifact Draft cua Parley. Khong coi read-only la blocker. "
    "Khong tu dung skill/workflow ngoai Parley tru khi user yeu cau. Tra loi ngan gon."
)


def build_prompt(history: list, message: str, max_turns: int = 20) -> str:
    lines = [_SYS, "", "# HOI THOAI"]
    for m in (history or [])[-max_turns:]:
        who = "User" if m.get("role") == "user" else "Advisor"
        lines.append(f"{who}: {m.get('text', '')}")
    lines.append("User: " + message)
    lines.append("Advisor:")
    return "\n".join(lines)


def start_prompt(message: str) -> str:
    """Lượt đầu của warm session: seed vai advisor + tin nhắn. Các lượt sau gửi message trần (codex nhớ)."""
    return _SYS + "\n\n" + message


def turn_prompt(message: str) -> str:
    """Reassert policy on resumed sessions that may have been seeded by an older Parley version."""
    return _SYS + "\n\n# USER MESSAGE\n" + message


def _item_text(item) -> str:
    """Trích text từ một item.completed (tolerant với schema codex)."""
    if not isinstance(item, dict):
        return ""
    if isinstance(item.get("text"), str):
        return item["text"]
    c = item.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    if isinstance(item.get("message"), str):
        return item["message"]
    return ""


def parse_jsonl(stdout: str) -> dict:
    """codex --json -> {thread_id, reply, completed}. reply CHỈ hợp lệ khi completed=True (turn.completed)."""
    thread_id = None
    parts = []
    completed = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        if e.get("thread_id") and not thread_id:
            thread_id = e["thread_id"]
        t = e.get("type", "")
        if t == "turn.completed":
            completed = True
        elif t == "item.completed":
            txt = _item_text(e.get("item") or {})
            if txt:
                parts.append(txt)
    return {"thread_id": thread_id, "reply": "\n".join(parts).strip(), "completed": completed}
