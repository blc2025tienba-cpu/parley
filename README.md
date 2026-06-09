# Parley

> Nơi các AI agent đàm đạo dưới sự điều phối của supervisor, người dùng theo dõi ẩn danh từ xa.

Parley để hai AI CLI tự làm việc **hands-free** trên một codebase: **Codex (A)** điều phối, **Kiro (B)** thực thi theo vai (analyzer → architect → coder → reviewer → fixer), một **Supervisor** giữ goal và gate mọi bước, còn bạn theo dõi realtime và can thiệp khi cần. Toàn bộ hội thoại ghi append-only để replay.

## Mô hình (pipeline SPARC bất đối xứng)

```
user goal --> Supervisor (governance) --gate--> Harness (chủ spawn, kill switch)
                                                    |
        A = Codex (orchestrator, read-only) <-------+-------> B = Kiro (executor, theo role)
        phát directive ở cuối stdout                          ghi report .md + trailer
```

- **Supervisor** = LLM-judge (approve/steer/stop) + lớp code tất định (harness). KHÔNG chạy shell tự do.
- **A = Codex Advisor** (`codex exec`, sandbox read-only): đọc và review mỗi report (`APPROVE|REJECT`), rồi đề xuất đúng MỘT directive `<<<DISPATCH|VERIFY|PHASE|COMPLETE>>>`.
- **B = Kiro** (`kiro-cli chat --agent <role>`): chạy đúng vai, ghi report `.md`, in trailer `<<<REPORT ...>>>`.
- **Supervisor** duyệt đề xuất của Advisor; khi `continue`, harness mới dispatch role/CLI đã được whitelist.
- Đơn vị công việc: **Phase → Slice**. Slice "done" = reviewer **APPROVE** + **verify** (build/test) exit 0.

Thiết kế đầy đủ: xem [`Parley-Implementation-Plan-v2.md`](./Parley-Implementation-Plan-v2.md) và [`Parley-PRD.md`](./Parley-PRD.md). Vận hành chi tiết: [`GUIDE.md`](./GUIDE.md).

## Yêu cầu
- Python 3.11+ (đã test 3.13).
- `codex` (codex-cli) và `kiro-cli` trên PATH, đã đăng nhập.
- `git`.
- (Tuỳ chọn, cho web monitor) `pip install -r requirements-web.txt` (fastapi + uvicorn).

Lõi Parley chỉ dùng thư viện chuẩn — không cần cài thêm để chạy `init`/`run`/test.

## Bắt đầu nhanh

```bash
# 1) Khởi tạo: trỏ vào thư mục project ĐÍCH (nên là nhánh git riêng/sandbox)
python -m parley.cli init D:\path\to\project --goal "Hoàn thành Phase 12: inbox sync"

# 2) Chạy vòng lặp hands-free
python -m parley.cli run

# 3) (Tuỳ chọn) Mở web monitor read-only
set PARLEY_TOKEN=doi-thanh-token-bi-mat
set PARLEY_DATA=./data
uvicorn parley.web.app:app --host 127.0.0.1 --port 8800
```

`init` tự: dò stack → sinh `verify.gates`, đồng bộ `.kiro/agents` (override agent global) vào project, tạo nhánh git làm việc, ghi `parley.config.json`.

## Dữ liệu sinh ra (`data_dir`, mặc định `./data`)
| File | Vai trò |
|------|---------|
| `conversation.ndjson` | Append-only changelog mọi lượt (replay/audit) |
| `status.json` | Trạng thái hiện tại (cây Phase→Slice) cho GUI |
| `control.json` | Lệnh observer (`continue\|pause\|steer\|stop`) |
| `decisions.ndjson` | Governance + xung đột supervisor/observer |
| `reports/` | Snapshot report `.md` + sha256 (self-contained) |

## Bảo mật (đọc trước khi chạy đêm)
- **R1 — edit roles chạy `--trust-all-tools` trên code THẬT**: chỉ trỏ `project_dir` vào **nhánh git riêng/sandbox**, KHÔNG production; giám sát vài vòng trước khi để qua đêm. Mỗi slice auto-commit để rollback. Read-only roles (analyzer/architect/reviewer) đã bị gỡ `shell` trong `.kiro/agents`.
- **R2 — web**: bind `127.0.0.1` + bearer token MỌI route; ra ngoài chỉ qua tunnel có danh tính (Tailscale/Cloudflare/SSH), KHÔNG port-forward trần.
- **Gate-cho-edit**: mọi dispatch tới role `edit:true` LUÔN qua supervisor (kể cả khi `every_n_turns>1`).
- **Kill switch + timeout** (idle/hard) thuộc harness, không phụ thuộc LLM. Dừng khi: COMPLETE | MAX_TURNS | stuck | loop | supervisor/observer stop.

## Phát triển & test
```bash
python -m unittest discover -s tests -p "test_*.py"
```
27 unit test (stdlib `unittest`) phủ protocol, channel, fsm, supervisor, gitio, harness loop, cli.

## Trạng thái
- ✅ P1 lõi tất định: protocol, channel, fsm, context, supervisor, gitio, agents, harness, cli — test xanh.
- ✅ Local agents `.kiro/agents` (override global, vá quyền tool).
- ⏳ Còn lại: live smoke với codex+kiro thật; micro-test `--trust-tools`; chạy web (cần fastapi).
