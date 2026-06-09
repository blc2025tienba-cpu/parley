# Parley — Hướng dẫn vận hành

Hướng dẫn từng bước để chạy Parley an toàn. Tổng quan: [`README.md`](./README.md). Thiết kế: [`Parley-Implementation-Plan-v2.md`](./Parley-Implementation-Plan-v2.md).

## 0. Chuẩn bị (một lần)
```bash
codex --version && codex login        # đảm bảo đã đăng nhập
kiro-cli --version && kiro-cli whoami
git --version
python --version                      # >= 3.11
```
Kiểm tra agent local đã có:
```bash
kiro-cli agent list                   # 6 agent của Parley phải hiện "Workspace"
```

## 1. INIT — bàn giao workspace
```bash
python -m parley.cli init <PROJECT_DIR> --goal "<mục tiêu>"
```
`init` làm (tất định, không gọi LLM chạy shell):
1. Dò stack (`package.json`/`Cargo.toml`/`pyproject`/`go.mod`) → `verify.gates`.
2. Sync `.kiro/agents` (bản canonical trong repo Parley) → `<PROJECT_DIR>/.kiro/agents`, thêm vào `.gitignore`.
3. Tạo/đổi sang nhánh git `parley/work` trong `<PROJECT_DIR>`.
4. Ghi `parley.config.json` (idempotent: chạy lại không ghi đè goal đang chạy).

**Trước khi chạy, mở `parley.config.json` và kiểm:**
- `project_dir` đúng là sandbox/nhánh git riêng — KHÔNG production.
- `verify.gates` đúng lệnh build/test thật của project (sửa nếu dò sai).
- `pipeline_mode`: `free` (chỉ log lệch — để quan sát A) hoặc `strict` (ép REJECT→fixer, chặn chuyển slice khi chưa APPROVE+verify_ok).
- `limits` (`max_turns`, `idle_timeout_s`, `hard_timeout_s`) hợp lý với build dài.
- `advisor_cmd`/`supervisor_cmd`: thêm `--model <...>` nếu muốn chốt model.

## 2. RUN — vòng lặp hands-free
```bash
python -m parley.cli run
```
Mỗi lượt: role phát report → Advisor review `APPROVE|REJECT` và đề xuất directive (kèm nonce harness cấp) → Supervisor gate → harness dispatch role/CLI đã whitelist hoặc chạy verify → ghi `conversation.ndjson` + cập nhật `status.json`. Một slice xong (reviewer APPROVE + verify exit 0) → auto-commit, ghi `slice_done`. Advisor phát `<<<COMPLETE>>>` → dừng.

**Khuyến nghị lần đầu:** đặt `max_turns` thấp (vd 10), ngồi xem vài vòng, rồi mới tăng.

Crash giữa chừng: chạy lại `parley run` — harness `resume` từ `conversation.ndjson`; nếu lượt cuối là dispatch chưa có report (edit dở) → trạng thái `paused`, dùng `git diff`/`git reset` quyết rồi chạy lại.

## 3. Giám sát + can thiệp (observer)
Bật web monitor (read-only mặc định):
```bash
pip install -r requirements-web.txt
set PARLEY_TOKEN=<token-bi-mat>
set PARLEY_DATA=./data
uvicorn parley.web.app:app --host 127.0.0.1 --port 8800
# mở http://127.0.0.1:8800 , nhập token, bấm connect
```
Truy cập từ xa: dựng tunnel có danh tính (Tailscale/Cloudflare/SSH) tới `127.0.0.1:8800`. KHÔNG mở port ra Internet.

Lệnh điều khiển (nút trên web, hoặc ghi thẳng `data/control.json`):
```jsonc
{"seq": 2, "verdict": "steer", "inject": "ưu tiên fix lỗi build trước"}
// verdict: continue | pause | steer | stop ; tăng "seq" mỗi lệnh mới
```
- `pause`: dừng ở ranh giới lượt, chờ lệnh `seq` mới.
- `steer`: chèn thông điệp lái vào dispatch kế.
- `stop`: dừng hẳn.
- Mâu thuẫn supervisor↔observer: **supervisor thắng**, ghi `conflict` vào `decisions.ndjson`.

## 4. Checklist an toàn trước khi chạy đêm
- [ ] `project_dir` là nhánh git riêng, làm việc sạch (`git status` clean), KHÔNG production.
- [ ] Đã chạy giám sát vài vòng, A phát directive đúng và kiro ghi report đúng path.
- [ ] `verify.gates` chạy được độc lập (`pnpm test`/`cargo test`… exit 0 khi code ổn).
- [ ] Web bind `127.0.0.1` + `PARLEY_TOKEN` đặt; chỉ truy cập qua tunnel.
- [ ] Biết cách dừng: ghi `control.json` `stop`, hoặc Ctrl-C harness (kill switch ở harness).
- [ ] `limits` đặt trần hợp lý; `pipeline_mode=strict` nếu muốn siết luồng.

## 5. Xử lý sự cố
| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
|-------------|------------------------|-----------|
| `turn_error` liên tục → `stopped: stuck` | A không phát directive đúng nonce/cú pháp | Xem `conversation.ndjson`; chỉnh prompt seed hoặc model A |
| Slice không bao giờ commit | Thiếu `<<<VERIFY>>>` hoặc verify fail | Kiểm `verify.gates`; A phải phát VERIFY cho slice |
| `stopped: loop` | Hai dispatch giống hệt liên tiếp | A kẹt — steer hoặc đổi model |
| Kiro không ghi report | Agent global đè local / sai cwd | `kiro-cli agent list` phải thấy "Workspace"; harness chạy kiro với cwd=project_dir |
| Web 401 | Sai/thiếu bearer token | Đặt `PARLEY_TOKEN` và nhập đúng trên trang |
| Build dài bị kill | `idle_timeout_s`/`verify.timeout_s` thấp | Tăng trong `parley.config.json` |

## 6. Dành cho người phát triển
```bash
python -m unittest discover -s tests -p "test_*.py"   # 27 test, không cần cài thêm
```
Module chính: `parley/harness.py` (vòng lặp), `parley/fsm.py` (verdict-gate), `parley/protocol.py` (parse directive/report), `parley/channel.py` (4 file dữ liệu), `parley/agents.py` (spawn + timeout), `parley/cli.py` (init/run). Deps được tiêm vào `Harness` nên test bằng fake-agent, không cần CLI thật.
