"""Parley planner (S3b, ADR-13).

Init phase (b): từ ideas/features → LLM (advisor/architect) đề xuất KẾ HOẠCH:
goal-list + execution_mode (sequential|parallel) + reason. User duyệt sau (S3c).
runner(prompt)->str được tiêm vào để test. Parse JSON phòng thủ.
"""
from __future__ import annotations

import json
import re

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _json_objects(s: str) -> list:
    """Trích các object {...} cân bằng ngoặc (hỗ trợ nested)."""
    objs, depth, start = [], 0, -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                objs.append(s[start:i + 1])
    return objs


def parse_plan(text: str) -> dict:
    """-> {execution_mode, goals:[{title}], reason}. Quét object cuối hợp lệ có 'goals'
    (né phần codex echo lại prompt/ví dụ JSON)."""
    clean = _ANSI.sub("", text or "")
    for cand in reversed(_json_objects(clean)):
        try:
            o = json.loads(cand)
        except Exception:
            continue
        if not isinstance(o, dict) or "goals" not in o:
            continue
        em = o.get("execution_mode", "sequential")
        if em not in ("sequential", "parallel"):
            em = "sequential"
        goals = []
        for it in (o.get("goals") or []):
            if isinstance(it, str):
                title = it
                description = ""
            elif isinstance(it, dict):
                title = it.get("title") or it.get("goal") or it.get("name") or ""
                description = it.get("description") or it.get("details") or it.get("scope") or ""
            else:
                title = ""
                description = ""
            title = str(title).strip()
            if title and title != "...":
                goals.append({"title": title[:200], "description": str(description).strip()[:12000]})
        return {"execution_mode": em, "goals": goals, "reason": str(o.get("reason", ""))}
    md = parse_markdown_plan(clean)
    if md["goals"]:
        return md
    return {"execution_mode": "sequential", "goals": [], "reason": "no-valid-json"}


def _markdown_headings(text: str, include_fenced: bool = False) -> list[str]:
    headings, in_fence = [], False
    for line in (text or "").splitlines():
        if not include_fenced and line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if (include_fenced or not in_fence) and re.match(r"^\s{0,3}#{2,6}\s+", line):
            headings.append(line.strip())
    return headings


def parse_markdown_plan(text: str) -> dict:
    """Fallback parser for Advisor markdown plans."""
    result = _parse_markdown_blocks(text, include_fenced=False)
    if result["goals"]:
        return result
    return _parse_markdown_blocks(text, include_fenced=True)


def _parse_markdown_blocks(text: str, include_fenced: bool) -> dict:
    lines, visible, in_fence = (text or "").splitlines(), [], False
    for line in lines:
        if not include_fenced and line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if include_fenced or not in_fence:
            visible.append(line)

    headings = []
    for i, line in enumerate(visible):
        if re.match(r"^\s{0,3}#{2,6}\s+", line):
            headings.append((i, line.strip()))

    task_goals = []
    task_matches = []
    for pos, h in headings:
        m = re.match(r"^#{2,6}\s+Task\s+(\d+)\s*[-–—:]\s*(.+?)\s*$", h, re.I)
        if m:
            task_matches.append((pos, m))
    for idx, (pos, m) in enumerate(task_matches):
        end = task_matches[idx + 1][0] if idx + 1 < len(task_matches) else len(visible)
        description = "\n".join(visible[pos + 1:end]).strip()
        task_goals.append({
            "title": f"Task {m.group(1)} - {m.group(2).strip()}"[:200],
            "description": description[:12000],
        })
    if task_goals:
        return {"execution_mode": "sequential", "goals": task_goals,
                "reason": "parsed from markdown task headings"}

    phase_goals = []
    phase_matches = []
    for pos, h in headings:
        m = re.match(r"^#{2,6}\s+([PG]\d+)\.?\s+(.+?)\s*$", h, re.I)
        if m:
            phase_matches.append((pos, m))
    for idx, (pos, m) in enumerate(phase_matches):
        end = phase_matches[idx + 1][0] if idx + 1 < len(phase_matches) else len(visible)
        phase_goals.append({
            "title": f"{m.group(1).upper()} - {m.group(2).strip()}"[:200],
            "description": "\n".join(visible[pos + 1:end]).strip()[:12000],
        })
    return {"execution_mode": "sequential", "goals": phase_goals,
            "reason": "parsed from markdown phase headings" if phase_goals else "no-valid-json"}


def plan(ideas, runner) -> dict:
    """ideas: str | list[str]. runner(prompt)->str (advisor/architect)."""
    items = ideas if isinstance(ideas, list) else [str(ideas)]
    prompt = (
        "Ban la PLANNER cua Parley. Doc IDEAS/FEATURES roi de xuat KE HOACH thuc thi: "
        "danh sach goal ngan gon theo thu tu hop ly + execution_mode + ly do.\n"
        'Tra ve DUY NHAT JSON mot dong: '
        '{"execution_mode":"sequential|parallel","goals":[{"title":"..."}],"reason":"..."}\n\n'
        "IDEAS:\n" + "\n".join("- " + str(i) for i in items)
    )
    return parse_plan(runner(prompt))
