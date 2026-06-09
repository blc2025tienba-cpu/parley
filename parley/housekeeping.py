"""Parley housekeeping (ADR-12, B1) — side-channel, artifacts trong project_dir.

- agent-git: LLM (model tối thiểu) SOẠN commit message từ diff. Harness mới chạy `git commit`.
- agent-document: cập nhật <project_dir>/CHANGELOG.md.
Cả hai dùng command-profile của `housekeeping.from_role` (coder) + `--model <minimal>`.
"""
from __future__ import annotations

import re

from .backends import CommandProfile

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def hk_profile(cfg) -> CommandProfile:
    """Command-profile housekeeping = cmd của from_role (coder) + --model tối thiểu."""
    base = list(cfg.roles[cfg.housekeeping.from_role].cmd)
    if cfg.housekeeping.model:
        base = base + ["--model", cfg.housekeeping.model]
    return CommandProfile(base, "housekeeping")


def _clean_line(stdout: str) -> str:
    """Lấy dòng nội dung cuối cùng (bỏ ANSI + dòng header '> ...')."""
    text = _ANSI.sub("", stdout or "")
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(">")]
    return lines[-1] if lines else ""


def suggest_commit_message(cfg, backend, slice: str, diff: str, fallback: str) -> str:
    """agent-git: soạn 1 dòng commit message từ diff. Fallback nếu tắt/không có diff/lỗi."""
    if not cfg.housekeeping.enabled or not diff:
        return fallback
    prompt = ("Ban la agent-git. Soan DUY NHAT mot dong commit message (<=72 ky tu, tieng Anh, "
              "conventional-commit style). CHI in dong message, khong giai thich.\n\n"
              f"slice: {slice}\nDIFF:\n{diff[:3000]}")
    out = backend.run_once(hk_profile(cfg), prompt, cfg.project_dir,
                           cfg.limits.idle_timeout_s, cfg.limits.hard_timeout_s).stdout
    msg = _clean_line(out)
    return (msg[:72] if msg else fallback)


def update_changelog(cfg, backend, slice: str, report_excerpt: str) -> bool:
    """agent-document: cập nhật CHANGELOG.md trong project_dir. True nếu đã chạy."""
    if not cfg.housekeeping.enabled:
        return False
    prompt = ("Ban la agent-document. Them MOT muc ngan (markdown) vao DAU file CHANGELOG mo ta "
              "thay doi cua slice; GIU nguyen noi dung cu.\n"
              f"slice: {slice}\nTrich report:\n{report_excerpt[:1000]}\n\n"
              f"Ghi vao file (trong thu muc lam viec): {cfg.housekeeping.changelog_path}")
    backend.run_once(hk_profile(cfg), prompt, cfg.project_dir,
                     cfg.limits.idle_timeout_s, cfg.limits.hard_timeout_s)
    return True
