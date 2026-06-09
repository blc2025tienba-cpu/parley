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

### Fixed (live-smoke findings, xem BACKLOG.md)
- **Parser khoan dung số dấu góc** (`protocol.py`): codex gpt-5.5 thỉnh thoảng emit thẻ mở
  rớt dấu (`slice="...">` thay vì `>>>`) → trước đây `protocol.parse` ra NONE → advisor stuck.
  Nới `_HEAD/_END/_REPORT` thành `<{2,3}...>{1,3}`. Verify trên raw log thật (NONE → DISPATCH).
- **Prompt file vào workspace** (`channel.write_prompt`): trước ghi `~/.parley/data/.../prompts`
  (ngoài project_dir) → sandbox provider (claude/cursor) không đọc được → `missing_report`.
  Nay materialize thêm bản trong `<project_dir>/.parley/prompts/` (path trao cho executor);
  giữ bản audit ở data_dir. `.gitignore` chặn `.parley/`.
- **Notify goal lẻ** (`notify._tick_push`): trước chỉ duyệt `contract.goal_ids` → goal chạy lẻ
  qua `/goals/{gid}/run` không bao giờ push Telegram. Nay union với `store.list_goals(pid)`.
- **Kiro read-only roles trust-tools** (LS-017, `cli.default_roles`): analyzer/architect/researcher
  cmd thêm `--trust-tools=fs_read` — trước thiếu nên kiro chặn ở "Tool approval required" dưới
  `--no-interactive` khi role cần đọc/grep file. `fs_read` = read/list/search; KHÔNG fs_write/bash.
- **Streaming live-log + pid** (LS-019, `backends._spawn`): mỗi spawn ghi stdout streaming ra
  `data_dir/live/<role>-<n>.log` (header `# pid=...`, flush từng dòng) song song buffer in-memory;
  endpoint `GET /goals/{gid}/live` trả last-line + mtime mỗi attempt → phân biệt "đang chạy" vs "treo"
  (attempt log cũ chỉ ghi sau khi xong → 0 byte khi timeout, vô dụng để theo dõi).
- **Task-split policy** (LS-018b, `context._POLICY`): hướng dẫn advisor chia dispatch theo phạm vi
  hẹp đúng role (analyzer khảo sát codebase nội bộ → report; architect đọc report + nghiên cứu ngoài →
  thiết kế), tránh gộp một dispatch khổng lồ vượt hard_timeout.

### Notes
- ADR-13 và ADR-15 đã live smoke (parser/preflight/fallback/notify/trust-tools xác nhận thật). ADR-14
  mới verify bằng unit test (chưa live smoke do provider hết quota); `warm_until_task_done` cho role
  B và ADR-16 (memory ledger / agentmemory) là đợt sau.
- LS-018a (claude opus `-p` timeout 0-byte) là giới hạn môi trường: opus không stream partial, workload
  reasoning lớn >hard_timeout → mất sạch output. Giảm thiểu bằng task-split + giữ hard_timeout 1800s.
- Full unit suite: 161/161 xanh.
