# Changelog

Tất cả thay đổi đáng chú ý của codebase Parley. Định dạng theo
[Keep a Changelog](https://keepachangelog.com/); ghi theo ADR (xem `Parley-Implementation-Plan-v2.md`).

## [Unreleased]

### Added
- **ADR-13 — Task entity & projection**: mỗi `dispatch` = một task instance (`task_id`,
  `origin`, `parent_task_id`, `title`); `tasks.project_tasks()` dựng view từ
  `conversation.ndjson` (không store riêng). UI hiển thị task card + trạng thái.
- **ADR-15 — Executor failure classification + provider fallback**:
  - `parley/executor.py`: phân loại lỗi 9 nhóm (`rate_limited / usage_exhausted /
    auth_error / permission_denied / account_suspended / cli_unavailable / timeout /
    cli_exit_error / missing_report`); signature kiểm tra **trước** `timed_out`.
  - Profile chain theo role + per-error fallback (retry → skip provider → next → stop sạch),
    thay vòng lặp REJECT đốt quota cũ. Chỉ report hợp lệ mới tới Advisor.
  - Stale-report protection (hash report-file trước/sau); claude named-agent preflight
    (`rglob ~/.claude/agents/**/<name>.md`); OpenCode spawn tuần tự (SQLite WAL lock);
    cursor qua argv prompt ngắn.
  - Events `executor_retry / executor_fallback / executor_error / executor_exhausted`
    (kèm `raw_excerpt`); raw log mỗi attempt → `data_dir/attempts/*.log`.
  - Advisor/Supervisor cũng có quota fallback (`classify_text` + `run_text_with_fallback`,
    chỉ trigger họ-quota).
- **ADR-14 — Warm sessions (advisor + supervisor, per-phase)**:
  - Advisor giữ warm session trong phase: turn đầu cold (`codex exec --json` + full seed),
    turn 2+ warm (`codex exec resume` + delta prompt, bỏ header/policy). `thread_id` lấy từ
    JSONL; output unwrap reply trước `protocol.parse`.
  - Supervisor warm qua `kiro --resume-id` (id học bằng `--list-sessions` diff) — **mặc định
    TẮT** (`supervisor_warm=False`) do list-diff chưa an toàn đa-tiến-trình.
  - Force-cold: `<<<PHASE>>>` mới, fallback sang provider ≠ primary, resume hỏng, và khi đạt
    `max_warm_turns_per_phase` (mặc định 20 → re-seed full context, không delta).
  - Hòa giải ADR-15: warm chỉ áp profile primary; mọi fallback → drop session.

### Notes
- ADR-13 và ADR-15 đã live smoke. ADR-14 mới verify bằng unit test (chưa live smoke do
  provider hết quota); `warm_until_task_done` cho role B và ADR-16 (memory ledger /
  agentmemory) là đợt sau.
- Full unit suite: 158/158 xanh.
