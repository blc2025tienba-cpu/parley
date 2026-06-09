# Implementation Plan — Parley

Tham chiếu: docs/Parley-PRD.md

## 1. Kiến trúc tổng thể
Hai tiến trình tách biệt, ghép qua file channel:

  [Orchestrator process] --append--> conversation.ndjson --tail--> [Web app] --SSE--> Browser
         |  ^                          status.json                      |
         |  |                          control.json <--POST------------ (observer controls)
         |  +-- đọc control.json tại ranh giới lượt
         +-- spawn builder A / builder B (headless) + gọi supervisor

Nguyên tắc: harness tất định sở hữu cơ chế (turn-taking, timeout, kill switch, guardrails);
supervisor LLM chỉ là cổng phán xét ngữ nghĩa.

## 2. Tech stack
- Lõi: Python 3.11+ (subprocess, threading/asyncio).
- Web: FastAPI + uvicorn; feed realtime bằng SSE; control bằng POST; 1 trang HTML + EventSource.
- LLM supervisor: SDK provider (openai/anthropic) hoặc gọi CLI.
- Truy cập từ xa: Tailscale / Cloudflare Tunnel / SSH (chốt ở Phase 0).

## 3. File channel — schema
conversation.ndjson (append-only, mỗi dòng 1 JSON, kết \n, UTF-8):
  {"id":1,"from":"user","to":"codex","text":"goal..."}
  {"id":2,"from":"codex","to":"kiro","text":"...","done":false}
status.json: {"turn":12,"current":"kiro","tokens":34512,"cost":0.42,"state":"running"}
control.json: {"verdict":"continue|pause|steer|stop","inject":"thông điệp lái (nếu steer)"}

## 4. Thành phần & hợp đồng

### 4.1 Agent adapter (headless, stdin, idle-timeout)
- API: run_agent(name, prompt, idle, hard) -> str
- Prompt qua STDIN, KHÔNG qua argv (giới hạn dòng lệnh Windows).
- Đọc stdout theo stream; reset đồng hồ mỗi khi có output; kill khi idle>idle hoặc tổng>hard.
- Cấu hình lệnh per-agent:
    codex: ["codex","exec"]                       # verify --help
    kiro : ["q","chat","--no-interactive", ...]    # verify --help

### 4.2 Context builder
- Ghép: [goal tĩnh + ràng buộc] + [rolling summary] + [N lượt gần nhất nguyên văn].
- Rolling summary do supervisor cập nhật để tránh phình token tuyến tính.

### 4.3 Supervisor gate
- Input: goal + conversation.ndjson (hoặc summary + N lượt cuối).
- Output JSON: {"verdict":"continue|steer|stop","reason":"...","inject":"..."}.
- Gọi mỗi lượt (hoặc mỗi 2–3 lượt để tiết kiệm). Harness hành động theo verdict.

### 4.4 Orchestrator loop (tất định)
- Luân phiên A/B; sau mỗi lượt: ghi ndjson, cập nhật status, đọc control.json, gọi supervisor.
- Dừng khi: [[END]] | MAX_TURNS | lặp giống nhau | verdict=stop | control=stop.
- pause: chờ tới khi control đổi. steer: chèn inject vào prompt lượt kế.

### 4.5 Web app
- GET /feed: SSE tail conversation.ndjson (đẩy dòng mới ~0.5s).
- GET /status: trả status.json.
- POST /control: ghi control.json (yêu cầu bearer token).
- GET /: trang HTML dùng EventSource hiển thị feed + nút pause/steer/stop.

### 4.6 Lớp bảo mật
- Bind 127.0.0.1; ra ngoài qua tunnel có danh tính.
- Middleware kiểm bearer token cho mọi request; bắt buộc cho /control.
- Không in secret ra feed/log; cân nhắc lọc/redact.

## 5. Guardrails (bắt buộc)
- MAX_TURNS cứng + token/cost cap (dừng khi vượt).
- Sandbox: chạy agent trong thư mục/nhánh git riêng, không phải repo thật.
- Hạn chế quyền tool; tránh --trust-all-tools mù.
- Phát hiện loop: 2 lượt output gần giống nhau → dừng.
- Kill switch nằm ở harness, không phụ thuộc supervisor.

## 6. Lộ trình theo phase
- Phase 0 — Spike: verify cờ non-interactive của codex/kiro (--help); xác nhận đọc stdin,
  in hết ra stdout rồi exit 0. Chốt kênh tunnel + model supervisor.
- Phase 1 — Harness lõi: run_agent (stdin + idle-timeout), loop luân phiên 2 agent,
  ghi conversation.ndjson + status.json, MAX_TURNS + sentinel. Chạy tay, có giám sát.
- Phase 2 — Supervisor gate: context builder + rolling summary + verdict JSON + steer/stop.
- Phase 3 — Web monitor: FastAPI /feed (SSE) + /status + trang HTML EventSource (read-only).
- Phase 4 — Controls + bảo mật: /control + control.json tại ranh giới lượt; bearer token;
  hướng dẫn tunnel (Tailscale/Cloudflare/SSH).
- Phase 5 — Guardrails & hardening: cost cap, phát hiện loop, sandbox, chạy thử qua đêm có log.

## 7. Kiểm thử
- Unit: context builder (cắt N lượt, summary), idle-timeout (giả lập tiến trình treo),
  parse verdict JSON, điều kiện dừng.
- Integration: agent giả (echo script) chạy trọn loop; control.json pause/steer/stop tác động đúng lượt.
- Security: request thiếu token bị 401; /feed không lộ khi chưa auth (nếu bật auth cho feed).
- Smoke: một task mẫu nhỏ với 2 CLI thật, MAX_TURNS thấp, cost cap thấp.

## 8. Cấu trúc thư mục đề xuất (project mới)
  parley/
    harness.py             # loop tất định + run_agent + guardrails
    agents.py              # cấu hình lệnh + adapter stdin/idle-timeout
    supervisor.py          # gọi LLM, trả verdict JSON
    context.py             # build prompt: goal + summary + N lượt
    web/app.py             # FastAPI: /feed (SSE), /status, /control, /
    web/index.html         # EventSource + controls
    parley.config.yaml     # timeouts, MAX_TURNS, cost cap, model, token
    data/                  # conversation.ndjson, status.json, control.json
    docs/                  # PRD + plan (copy 2 file này vào)

## 9. Quyết định cần xác nhận khi bắt đầu build
- Python thuần (đã chọn). Tunnel nào? Model supervisor? Giữ phiên ấm hay gửi lại transcript?

## 10. Quy ước định danh
- Repo / thư mục gốc: parley
- CLI: `parley run --goal "..."`, `parley watch` (mở web monitor)
- Config: parley.config.yaml
- Package Python: parley/
