# Parley — Live-Smoke Backlog & Edge-Case Log

Nơi ghi **issue/edge-case phát hiện trong quá trình live smoke và vận hành thực tế**
(khác với §19 backlog kế hoạch P1/P2 trong `Parley-Implementation-Plan-v2.md`, vốn là
roadmap tính năng). Mỗi mục: trạng thái, nơi phát hiện, mô tả, hướng xử lý.

Trạng thái: `OPEN` (chưa xử lý) · `FIXED` (đã sửa) · `MITIGATED` (giảm thiểu, chưa dứt điểm) · `WONTFIX` · `INVESTIGATING`.

---

## Đã sửa (FIXED)

### LS-001 · Claude preflight chặn oan (FIXED 2026-06-09)
- **Phát hiện**: live smoke ADR-15, goal research agentmemory — analyzer fallback sang claude báo `cli_unavailable` dù claude chạy được.
- **Nguyên nhân**: `_claude_agent_available` dùng `glob("{agent}.*")` chỉ quét top-level + cần đuôi; agent thật nằm subdir `.md` (`~/.claude/agents/analysis/code-analyzer.md`).
- **Sửa**: đổi sang `rglob("{agent}.md")` (đệ quy). Test `code-analyzer/architecture/researcher → True`.

### LS-002 · Không có raw log mỗi attempt → khó debug misclassification (FIXED 2026-06-09)
- **Phát hiện**: khi LS-001 phân loại sai, không có cách xem CLI thật in gì.
- **Sửa**: `Attempt.raw_excerpt` + ghi raw đầy đủ ra `data_dir/attempts/<role>-<n>-<provider>-<model>-<reason>.log`. UI hiện raw_excerpt + hover.

### LS-003 · Timeout fail-fast thay vì retry (FIXED 2026-06-09)
- **Phát hiện**: ví dụ thực `Retrying... attempt 4/10 · API_TIMEOUT_MS` — timeout là transient.
- **Sửa**: `TIMEOUT: STOP → NEXT_PROFILE`; tập `RETRYABLE=(rate_limited, timeout)`. Text role: chain toàn-timeout không hard-fail (trả về cho consec_err xử lý).

### LS-004 · usage_exhausted bỏ sót chuỗi cursor (FIXED 2026-06-09)
- **Phát hiện**: test `agent --model gpt-5.5-high` → account cursor trả `"You've hit your usage limit ... usage limits will reset"`, signature cũ không match.
- **Sửa**: thêm `hit your usage limit | usage limits will reset | spend limit` vào `usage_exhausted`.

### LS-005 · Warm codex JSONL không unwrap → directive luôn NONE (FIXED 2026-06-09)
- **Phát hiện**: unit test ADR-14 — codex `--json` bọc directive trong JSONL (newline escape), `protocol.parse` trên raw → NONE.
- **Sửa**: warm+codex unwrap `parse_jsonl → reply` (newline thật) cho downstream; classify vẫn chạy raw.

### LS-008 · Codex emit directive rớt dấu `>` → NONE (FIXED 2026-06-09)
- **Phát hiện**: live smoke goal_5d770bff — raw log cho thấy codex chạy XONG, ra đầy đủ directive + `<<<END>>>`, nhưng thẻ mở kết thúc bằng MỘT `>` (`slice="...">`) thay vì `>>>`. `protocol.parse` (regex cứng `>>>`) → NONE. KHÔNG phải lỗi timeout/reconnect như chẩn đoán ban đầu.
- **Nguyên nhân thật**: gpt-5.5 hay rớt ký tự LẶP (`>>>`→`>`); `Reconnecting N/5` (SSE retry, codex#27185) là hiện tượng song song, không phải nguyên nhân NONE.
- **Sửa**: nới 3 regex `_HEAD/_END/_REPORT` → `<{2,3}...>{1,3}` (khoan dung số dấu góc, neo cả dòng nên ~0 false-match). Verify trên raw log thật: NONE → DISPATCH. Test cũ `<<<>>>` vẫn khớp + 2 test rớt-dấu mới.
- **Token có tên** (`[[PARLEY:...]]` thay `<<<>>>`) ghi nhận là cải tiến tương lai (robust hơn với LM rớt ký tự) nhưng cần đo LM-compliance + đổi mọi builder/test → ADR riêng.

### LS-011 · Goal lẻ (ngoài contract) không push Telegram (FIXED 2026-06-09)
- **Phát hiện**: goal chạy qua `/goals/{gid}/run` đứng một mình → `_tick_push` chỉ duyệt `contract.goal_ids` → không push.
- **Sửa**: `_tick_push` union `contract.goal_ids ∪ store.list_goals(pid)` cho phần push events (giữ contract-only cho transition i/n). Live verify: goal lẻ goal_2859b601 → topic 4779 tạo, push tới event cuối.

### LS-016 · Prompt file ở data_dir → sandbox provider (claude/cursor) không đọc được (FIXED 2026-06-09)
- **Phát hiện**: live smoke — analyzer fallback claude báo `permissions error ... cannot read PROMPT_PATH C:\Users\MYPC\.parley\data\...\prompts\task.md` → `missing_report`.
- **Nguyên nhân**: `write_prompt` ghi vào `data_dir` (`~/.parley/data/...`) NGOÀI project_dir; claude/cursor sandbox chỉ đọc trong workspace. Report files dùng path relative project_dir nên không bị (bất đối xứng).
- **Sửa**: `write_prompt(.., project_dir=)` materialize prompt vào `<project_dir>/.parley/prompts/` (trong workspace) và trả path đó cho executor; vẫn giữ bản audit ở data_dir. `.gitignore` chặn `.parley/`. Live verify: kiro đọc được ("Successfully read 1213 bytes").

### LS-017 · Kiro read-only roles thiếu `--trust-tools=` → chết khi cần tool (FIXED 2026-06-10)
- **Phát hiện**: live smoke goal_2859b601 — analyzer (kiro) đọc được prompt rồi chết: `error: Tool approval required but --no-interactive was specified`. Read-only roles cần `fs_read`/grep để khảo sát nhưng cmd thiếu trust → chặn (chết KỂ CẢ khi không rate-limit).
- **Sửa**: `default_roles()` read-only (analyzer/architect/researcher) cmd thêm `--trust-tools=fs_read` — chỉ đọc/list/search file, KHÔNG `execute_bash`/`fs_write` (read-only role giữ read-only). Coder/fixer (edit, dùng opencode) không đụng.

### LS-018 · Claude opus `-p` timeout 0-byte + task scope quá rộng (FIXED 2026-06-10)
- **Phát hiện**: analyzer-02/03-claude-opus = 0 byte/timeout. Opus `-p` không stream; task gộp (khảo sát codebase 4093 dòng + nghiên cứu repo ngoài + plan) >15ph → hard_timeout kill trước khi in → mất sạch.
- **Sửa**: (a) giữ `hard_timeout` mặc định 1800s (không tự cap 900s như smoke trước); (b) `_POLICY` (context.py) siết advisor CHIA task hẹp đúng role — analyzer chỉ khảo sát nội bộ, architect nghiên cứu+thiết kế, không gộp; mỗi dispatch đủ nhỏ để in report trước timeout. Streaming live-log (LS-019) cho partial output khi bị kill.

### LS-019 · Observability: PID + live streaming log (FIXED 2026-06-10)
- **Phát hiện**: claude chạy 15ph im lặng, không phân biệt "đang chạy" vs "treo"; attempt log chỉ ghi SAU khi xong (0 byte nếu timeout).
- **Sửa**: `backends._spawn(live_log=)` ghi stdout STREAMING ra `data_dir/live/<role>-<n>.log` (flush mỗi dòng) + header `# pid=... start=...` + footer `# end exit=...`. Executor truyền live_log mỗi attempt (qua `_run_once` an toàn với fake backend cũ). Endpoint `GET /goals/{gid}/live` trả last-line + mtime + pid mỗi attempt cho UI/curl tail real-time.

### LS-020 · Read-only role không ghi được report file (FIXED 2026-06-10, hướng B)
- **Phát hiện** (user test 3 provider): read-only mode (kiro `fs_read`, claude `plan`, cursor `plan`) cấm ghi file → role read-only không hoàn thành report-trailer protocol. Cả 3 provider đều không tạo được report.
- **Sửa (hướng B — user chốt)**: cho read-only role (analyzer/architect/researcher/reviewer) **quyền ghi thật** ở tầng CLI: kiro `--trust-all-tools`, claude `--dangerously-skip-permissions`, cursor `--force`, reviewer opencode `plan→build`. Cưỡng chế read-only ở CLI bỏ; thay bằng **ràng buộc MỀM**: `context._scope_block()` thêm `# SCOPE` vào role prompt — read-only role CHỈ được ghi REPORT_PATH, CẤM sửa source; advisor review diff là lớp chặn. Cờ role `edit` vẫn False (harness không feed diff/verify như edit role). Prompt cũng yêu cầu đọc `AGENTS.md` đồng bộ contract.
- **Đánh đổi**: an toàn chuyển từ cứng (CLI chặn) sang mềm (prompt + review). Read-only role kỹ thuật CÓ THỂ sửa source — chặn bằng SCOPE + advisor REJECT + git (chưa auto-commit khi chưa done).

### LS-021 · Trailer giả được chấp nhận dù report file không tồn tại (FIXED 2026-06-10)
- **Phát hiện** (user): `classify(has_trailer=True, file_changed=False) -> None` — role chỉ cần IN `<<<REPORT>>>` là success kể cả không ghi file → Advisor review report rỗng (`snapshot_path=None`).
- **Sửa**: `classify(..., report_present)` — trailer chỉ hợp lệ khi file report **tồn tại + non-empty** (`executor.report_present()` check ở call site). Phantom trailer (trailer nhưng không file) → `MISSING_REPORT` → STOP sạch (không feed phantom cho Advisor).

### LS-022 · `permission_denied` STOP → không fallback (FIXED 2026-06-10)
- **Phát hiện** (user): kiro bị chặn ghi → `permission_denied` → STOP cứng, không thử claude/cursor.
- **Sửa**: `_ACTION[PERMISSION_DENIED] = NEXT_PROFILE` (thử profile/mode kế; provider khác có thể ghi được). Gắn với LS-020: giờ mọi profile write-capable nên permission-denied hiếm hơn, nhưng nếu xảy ra thì fallback thay vì chết.

---

## Giảm thiểu (MITIGATED)

### LS-006 · kiro `--list-sessions` diff không an toàn đa-tiến-trình (MITIGATED 2026-06-09)
- **Phát hiện**: thiết kế supervisor warm (ADR-14). Mỗi goal = process riêng; 2 goal cùng cwd → list-diff bắt nhầm session id.
- **Giảm thiểu**: TẮT `supervisor_warm` mặc định (`supervisor_warm=False`). Code warm giữ nguyên.
- **Dứt điểm (OPEN)**: cần **file-lock theo resolved cwd** (không phải dict in-memory) hoặc lọc session theo nội dung (match goal/agent trong prompt đầu) để bật lại an toàn.

---

## Chưa xử lý (OPEN)

### LS-007 · ADR-14 warm chưa live smoke thật
- **Phát hiện**: 2 lần thử goal research đều không chạy tới warm turn (codex flaky + provider hết quota).
- **Cần**: provider hồi quota → chạy goal nhiều turn, xác nhận turn-2 token (warm delta) < turn-1 (cold seed), `session_ref` giữ qua turn, force-cold đúng khi chạm `max_warm_turns_per_phase`.

### LS-009 · Prompt tiếng Việt bị mojibake trong stdin codex
- **Phát hiện**: raw log advisor — `Khảo sát` → `Kháº£o sÃ¡t` trong stdin codex (encoding Windows).
- **Tác động**: có thể làm agent confused. Cần kiểm tra encoding stdin pipe (`_spawn` dùng `encoding="utf-8"` cho stdout; stdin write cần xác nhận UTF-8).

### LS-010 · `PUT /goals/{gid}/config` clobber toàn bộ config
- **Phát hiện**: khi set `max_turns` qua API, config bị ghi đè chỉ còn `{limits}` — mất `roles`/`advisor_cmd`/`fallbacks`.
- **Tác động**: re-run goal fail (`KeyError: roles`). Phải rebuild config thủ công.
- **Cần**: endpoint PUT config nên **merge** thay vì replace, hoặc validate đủ key bắt buộc.

### LS-012 · `project_init` self-copy crash khi project_dir == repo Parley
- **Phát hiện**: tạo project trỏ vào chính `D:\Projects\Parley` → `sync_agents` copy `.kiro/agents` đè lên chính nó → `SameFileError`.
- **Workaround đã dùng**: set `initialized=True` thủ công, bỏ qua `project_init`.
- **Cần**: `sync_agents` skip khi `src == dst` (so sánh resolved path).

### LS-013 · Reset goal không xóa conversation.ndjson → resume nhầm
- **Phát hiện**: reset goal `failed→idle` rồi re-run; harness `ch.resume()` thấy dispatch cũ chưa có report → vào chế độ paused thay vì chạy mới.
- **Workaround**: tạo goal mới (conversation trắng) thay vì re-run goal cũ.
- **Cần**: định nghĩa rõ "re-run goal" = xóa/archive conversation cũ hay resume tiếp; hiện mơ hồ.

---

## Mở rộng còn thiếu (theo thiết kế, chưa làm)

### LS-014 · `warm_until_task_done` cho role B (executor) — chưa implement
- ADR-14 đợt này chỉ làm advisor + supervisor per-phase. Role B (analyzer/architect/coder/fixer/reviewer) vẫn cold mỗi dispatch.

### LS-015 · ADR-16 memory ledger / agentmemory — chưa implement
- Cầu nối cross-session/provider khi warm chết (cold sau fallback / goal mới). Quyết định đã chốt: ledger ghi `docs/`, nối agentmemory thật — nhưng cần dựng agentmemory service (port 3111) trước.
