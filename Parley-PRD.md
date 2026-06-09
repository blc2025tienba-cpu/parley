# PRD — Parley

> Parley — nơi các AI agent đàm đạo dưới sự điều phối của supervisor, người dùng theo dõi ẩn danh từ xa.

## 1. Bối cảnh & Vấn đề
Khi vibecode với nhiều model từ nhiều provider (vd codex/gpt-5.5 và kiro-cli/opus-4.8),
mỗi AI chạy ở một terminal riêng. Hiện phải copy-paste thủ công output của AI này
làm input cho AI kia. Cần một hệ tự động trao đổi giữa 2 AI khi không có người ở máy,
có một agent giám sát giữ đúng hướng, và một bảng theo dõi từ xa để người dùng quan sát ẩn danh.

## 2. Mục tiêu (Goals)
- Hai AI agent tự trao đổi (turn-by-turn) hands-free để cùng hoàn thành một task/goal.
- Một supervisor agent đặt/giữ goal, theo dõi hội thoại, lái hướng hoặc dừng khi đi sai.
- Người dùng theo dõi realtime từ xa, mặc định chỉ đọc (ẩn danh), can thiệp khi cần.
- Toàn bộ hội thoại lưu dạng changelog (append-only) để xem lại/replay.

## 3. Không thuộc phạm vi (Non-goals)
- Không tự động hóa copy-paste giữa các terminal tương tác (TTY automation).
- Không huấn luyện/fine-tune model. Không hỗ trợ >2 builder agent ở v1.
- Không phải sản phẩm đa người dùng; đây là công cụ cá nhân.

## 4. Người dùng & vai trò
- Builder Agent A: codex (gpt-5.5) — chạy headless non-interactive.
- Builder Agent B: kiro-cli (opus-4.8) — chạy headless non-interactive.
- Supervisor Agent: model bất kỳ (có thể rẻ hơn) — chấm điểm ngữ nghĩa, trả verdict.
- Observer (con người): theo dõi từ xa, mặc định read-only; có thể pause / steer / stop.

## 5. User stories
- Là người dùng, tôi đặt một goal + context, rồi rời máy; 2 AI tự làm việc tới khi xong.
- Là người dùng, tôi mở trình duyệt từ xa và xem hội thoại chạy realtime.
- Là người dùng, khi thấy lệch hướng tôi gửi một thông điệp lái hoặc bấm stop.
- Là supervisor, sau mỗi lượt tôi kiểm tra tiến độ so với goal và quyết định tiếp/lái/dừng.

## 6. Yêu cầu chức năng (FR)
- FR1: Harness luân phiên gọi 2 builder agent ở chế độ non-interactive, mỗi lượt một lần chạy.
- FR2: Prompt truyền qua STDIN (không qua argv) để chịu được prompt rất dài.
- FR3: Context mỗi lượt = goal tĩnh + rolling summary + N lượt gần nhất (cấu hình N).
- FR4: Supervisor được gọi tại ranh giới lượt, trả JSON {verdict, reason, inject}.
- FR5: Harness (code tất định) sở hữu kill switch; supervisor chỉ tư vấn.
- FR6: Mọi lượt ghi append-only vào conversation.ndjson + cập nhật status.json.
- FR7: Web app phát feed realtime (SSE) và nhận lệnh điều khiển (pause/steer/stop) qua POST.
- FR8: Lệnh điều khiển ghi vào control.json, harness đọc tại ranh giới lượt.
- FR9: Điều kiện dừng: sentinel [[END]], MAX_TURNS, lặp 2 lượt giống nhau, hoặc observer stop.

## 7. Yêu cầu phi chức năng (NFR)
- Bảo mật: web mặc định bind 127.0.0.1; truy cập từ xa qua tunnel có danh tính
  (Tailscale/Cloudflare Tunnel/SSH); bearer token cho mọi request, bắt buộc cho /control.
  Không port-forward trần. Hội thoại có thể chứa code/secret → không log lộ secret.
- Tin cậy: idle-timeout (reset khi có output) + absolute ceiling; orchestrator tách
  khỏi web để cô lập lỗi (web sập agent vẫn chạy).
- Chi phí: MAX_TURNS cứng + token/cost cap; cảnh báo khi vượt ngưỡng.
- An toàn vận hành: chạy trong sandbox/nhánh git riêng; không bật --trust-all-tools
  mù; chạy có giám sát vài vòng trước khi để qua đêm.
- Tính di động: Windows-first (UTF-8, NDJSON framing \n), không phụ thuộc TTY.

## 8. Rủi ro chính
- R1 (cao): agent có quyền tool tự duyệt cho nhau chạy lệnh phá hoại khi không người trông.
  → Giảm thiểu: sandbox, hạn chế quyền tool, kill switch tất định.
- R2 (cao): web phơi ra mạng không auth → lộ code/secret, bị chiếm quyền vòng lặp.
  → Giảm thiểu: tunnel + token + bind localhost.
- R3 (trung bình): loop vô tận đốt token. → MAX_TURNS + cost cap + phát hiện lặp.
- R4 (trung bình): cờ CLI non-interactive khác giả định. → verify bằng --help ở Phase 0.

## 9. Tiêu chí thành công
- Chạy trọn một task mẫu hands-free, dừng đúng điều kiện, không vượt cost cap.
- Observer xem realtime từ xa qua tunnel có auth; pause/steer/stop tác động đúng lượt kế.
- conversation.ndjson replay lại được toàn bộ phiên.

## 10. Câu hỏi mở (cần chốt trước khi build)
- Cờ non-interactive thật của codex và kiro-cli (--help).
- Có cần giữ phiên "ấm" (PTY) không, hay gửi lại transcript là đủ? (mặc định: đủ)
- Kênh truy cập từ xa: Tailscale / Cloudflare Tunnel / SSH?
- Model dùng cho supervisor.
