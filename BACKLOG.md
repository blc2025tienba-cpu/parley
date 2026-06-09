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

### LS-017 · Kiro read-only roles thiếu `--trust-tools=` → chết khi cần tool
- **Phát hiện**: live smoke goal_2859b601 — analyzer (kiro) đọc được prompt, nhưng khi cần tool: `error: Tool approval required but --no-interactive was specified. Use --trust-all-tools`.
- **Nguyên nhân**: `default_roles()` analyzer/architect/researcher cmd = `kiro-cli chat --no-interactive --agent <role>` — KHÔNG có `--trust-tools=`/`--trust-all-tools`. Supervisor có `--trust-tools=` (judge không cần tool); read-only roles cần `fs_read`/grep để khảo sát → bị chặn. Sẽ chết KỂ CẢ khi không rate-limit.
- **Cần (quyết định bảo mật)**: read-only roles chỉ cần đọc → thêm `--trust-tools=fs_read` (hoặc tool an toàn cụ thể), KHÔNG `--trust-all-tools` (read-only role không nên ghi/shell). Coder/fixer (edit) cần scope rộng hơn — đã có chain riêng. Cần chốt tool-set cho từng tier.

### LS-018 · Claude opus `-p` timeout 0-byte với workload reasoning lớn
- **Phát hiện**: live smoke — analyzer-02/03-claude-opus-4.8 = 0 byte, reason=timeout. Opus `-p` không stream, reasoning (phân tích cả codebase + repo ngoài) >15ph → hard_timeout 900s kill trước khi in → 0 byte → missing_report → chain claude tiếp.
- **Bản chất**: giới hạn thời gian/tài nguyên + opus không stream từng phần. Không phải lỗi logic.
- **Cần cân nhắc**: (a) tăng hard_timeout cho read-only analysis role; (b) `--output-format stream-json` nếu claude hỗ trợ để có partial output (gắn với LS-019 observability); (c) chia nhỏ task analyzer.

### LS-019 · Thiếu observability cho process task đang chạy (PID + live tail)
- **Phát hiện**: khi claude chạy 15ph im lặng, không phân biệt được "đang làm việc" vs "treo"; attempt log chỉ ghi SAU khi xong (0 byte nếu timeout → vô dụng để theo dõi live). UI fallback không show pid.
- **Đề xuất (user)**: nhận dạng mỗi process qua **PID** + emit lên event; **ghi temp log streaming** mỗi process; **tail log → UI show last-line + timestamp** để theo dõi real-time.
- **Cần**: sửa `backends._spawn` ghi stdout streaming ra temp file (không chỉ buffer in-memory); emit `pid` trong dispatch/fallback event; notify/UI đọc tail. Việc đáng kể, làm riêng.

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
