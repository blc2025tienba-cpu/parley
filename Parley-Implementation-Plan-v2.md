# Implementation Plan v2 — Parley (Detailed Design for Architect Review)

> Bản thiết kế chi tiết, tinh chỉnh sau phiên phân tích nghiệp vụ 2026-06-01.
> Tham chiếu: `Parley-PRD.md`, `Parley-Implementation-Plan.md` (bản gốc, mức cao).
> Trạng thái: P1 LIVE-SMOKE PASSED (codex+kiro, 2026-06-07) — orchestrate→gate(governance sống)→spawn→report→verify→COMPLETE, tạo file thật. 71 unit test. ADR-01..14 đang được chốt dần.

## 0. Mục đích tài liệu
Nắm bắt mô hình nghiệp vụ + dataflow + hợp đồng dữ liệu + thiết kế module đã thống nhất,
để Architect rà soát trước khi viết code. Mọi điểm LỆCH so với PRD gốc được đánh dấu rõ.

---

## 1. Thay đổi cốt lõi so với PRD gốc (PHẢI ĐỌC TRƯỚC)
PRD gốc giả định "2 AI peer ngang hàng, luân phiên ping-pong A->B->A->B + 1 supervisor".
Phân tích workflow THẬT (bằng chứng: `D:\LoriBrowser\lori-browser-0.22.3-c.01\docs\reports`,
naming `phase12-slice-{a..k}-{coder|reviewer|orchestrator}-report.md`) cho thấy mô hình đúng là
**pipeline SPARC bất đối xứng**:

- KHÔNG phải ping-pong. A (Codex) ĐIỀU PHỐI một chuỗi lần-gọi-B-theo-vai.
- B (Kiro) đóng NHIỀU vai trong cùng một slice: analyzer -> architect -> coder -> reviewer -> orchestrator -> fixer.
- "Verdict" là first-class, có HAI lớp (xem 2 & 6.6).
- "Contract" = bộ tài liệu plan/specs/checklist, không phải một field nhỏ trong goal.
- Context của A khóa theo Phase (Feature), reset mỗi Phase mới.

Architect cần xác nhận sự lệch này là chủ ý và đúng với cách vận hành mong muốn.

---

## 2. Ba lớp tác nhân
| Lớp | Tên | Vai trò | Quyền |
|-----|-----|---------|-------|
| Governance | **Supervisor** | Giữ goal/vision tổng thể; gate mọi intent của A; ra verdict approve/steer/stop + xác nhận role hợp lệ. Thắng observer khi mâu thuẫn (phải log). | LLM-judge — KHÔNG shell/spawn (harness spawn, ADR-06) |
| Orchestrator cấp cao | **A = Codex** (warm theo Phase) | Đọc report nguyên văn -> điều phối đúng task tới đúng role -> ra prompt chuẩn bám contract. | Viewer (KHÔNG edit code) |
| Executor | **B = Kiro** | Chạy đúng role qua `kiro-cli chat --agent <role>`; đọc/ghi file project bằng tool riêng; đẻ report `.md`. **Cold lần đầu mỗi task; warm khi chỉnh sửa cùng task** (ADR-14). | Edit CHỈ khi role có `--trust-all-tools` |

Điểm an toàn quan trọng: edit-rights theo ROLE, không theo agent. Chỉ role có `--trust-all-tools`
(orchestrator/coder/fixer) mới sửa code. A và các role read-only (analyzer/architect/reviewer) không sửa.

### 2.1 Spawn ownership — harness spawn, supervisor thuần LLM-judge (ADR-06)
User muốn supervisor là người "khởi động" Kiro. Để KHÔNG tái tạo rủi ro R1 và có trust boundary rõ:
- **LLM-supervisor**: đọc goal+contract+intent -> phán quyết approve/steer/stop + xác nhận role hợp lệ. KHÔNG chạy shell.
- **Harness (code tất định)**: chủ DUY NHẤT của mọi subprocess — spawn (chỉ từ whitelist), kill switch, timeout.

Tức là approve của supervisor = "bấm nút chạy" (cấp phép), còn harness mới thực sự spawn lệnh đã định sẵn.
Không có "lớp code của supervisor" tách riêng đi spawn — gom hết điểm chạm subprocess vào harness.

---

## 3. Đơn vị công việc & vòng đời
- Đơn vị: **Phase -> Slice -> (sub-slice)**. Một slice đi qua CHUỖI role, không luân phiên đối xứng.
- Done của slice = build/test ok + reviewer **APPROVE**.
- Done toàn cục = A phát `<<<COMPLETE>>>` (tương đương `[[END]]`).
- REJECT -> quay lại fixer; APPROVE mới sang slice kế. A quyết chuyển slice/phase.
- Cuối mỗi Phase bắt buộc có `final-reconciliation-report` (đã thấy ở project mẫu).

---

## 4. Dataflow (đã sửa — KHÔNG phải ping-pong)
```
user issue
   |
   v
A (Codex, warm/Phase) --intent{role,prompt,profile}--> Supervisor gate (LLM judge: approve/steer/stop)
                                                            | approve
                                                            v
                                  Harness (CHU spawn): whitelist + kill + timeout (ADR-06)
                                                            |
                                                            v
                                  spawn: kiro-cli --agent <role>
                                                            |  (STDIN ngắn: PROMPT_PATH -> data_dir/prompts/<task_id>.md)
                                                            v
                          Kiro (role) --doc/ghi project, chay test--> report-*.md
                                                            |  (stdout trailer + file)
   +--------------- report nguyen van <----------------------+
   v
A doc report -> quyet buoc ke (role khac / fix / sang slice) ... lap den khi:
   reviewer APPROVE + build/test ok + A phat <<<COMPLETE>>> -> done

Song song:  moi luot -> append conversation.ndjson + cap nhat status.json (cay Phase->Slice->Task)
            observer --POST--> control.json --(doc o ranh gioi luot)--> Supervisor
```

---

## 5. Business rules
1. Vòng lặp = A điều phối chuỗi lần-gọi-B-theo-vai (không ping-pong).
2. Edit-rights theo role (`--trust-all-tools`). A luôn viewer.
3. REJECT -> fixer; APPROVE -> slice kế. A quyết chuyển Phase, phát `<<<COMPLETE>>>` toàn cục.
4. Stop: `<<<COMPLETE>>>` | MAX_TURNS | lặp 2 lượt giống HỆT (so chuỗi chính xác) | supervisor stop | observer stop.
5. Pause: chạy hết lượt hiện tại rồi mới dừng; chờ control đổi (seq mới).
6. B **cold lần đầu mỗi task**; **warm khi chỉnh sửa cùng task** (ADR-14). Lượt cold: Harness ghi prompt tự đủ nghĩa vào `data_dir/prompts/<task_id>.md`, B chỉ nhận prompt ngắn có `PROMPT_PATH` + trỏ contract/report; lượt warm: delta (reject note / steer / chỉnh plan) + pointer file. Task `done` → đóng session → lần dispatch kế **bắt buộc cold**.
7. Context A khóa theo Phase: **warm session** trong Phase (ADR-14); Phase mới → reset session A, seed lại
   = goal + contract Phase mới + reconciliation report Phase trước (carryover).
8. Supervisor thắng observer khi mâu thuẫn, ghi sự kiện vào `decisions.ndjson`.
9. Token/cost: không ưu tiên ở v1.

---

## 6. Hợp đồng dữ liệu (6 contract)

### 6.1 Config + command-profile — `parley.config.yaml`
```yaml
project_dir: D:\LoriBrowser\lori-browser-0.22.3-c.01   # sandbox dich (KHONG tro production)
data_dir: ./data
contract_path: null                        # neo plan/specs; co the null luc dau -> A dispatch analyzer/architect tao (R2-7/ADR-01)
limits:   { max_turns: 200, hard_iter_cap: 500, max_turn_errors: 5, idle_timeout_s: 120, hard_timeout_s: 1800 }  # hard_iter_cap/max_turn_errors chong A spin (R2-10)
context:  { boundary: phase }              # reset session A moi Phase moi
pipeline: { mode: free }                   # free | strict (FSM verdict-gate, ADR-03)
verify:                                    # nhieu cong; PASS = TAT CA exit 0 (ADR-04/R2-4)
  timeout_s: 600                           # timeout rieng, tach idle role (ADR-09)
  gates:
    - ["pnpm","exec","tsc","--noEmit"]
    - ["pnpm","vitest","run"]
    - ["cargo","test"]
    - ["pnpm","exec","biome","check","."]
supervisor: { model: "<re>", every_n_turns: 1 }   # 1 = gate moi luot (an toan); >1 = tiet kiem, luot bo qua mac dinh continue (R2-5)
web:      { bind: 127.0.0.1, port: 8800, token_env: PARLEY_TOKEN }

session:                                              # ADR-14 — policy transport; FSM/governance KHONG phu thuoc
  advisor: warm_per_phase                             # A (Codex orchestrator)
  init_advisor: warm_per_project                      # Init chat + planner (project-level)
  supervisor: cold                                    # gate ngan, moi luot doc lap
  executor_default: warm_until_task_done              # TAT CA role B (analyzer|architect|reviewer|coder|fixer)
  max_warm_turns_per_task: 8                          # vuot -> force cold + session_end
  force_cold_on: [slice_change, phase_change, role_change, task_done]

advisor:  { cmd: ["codex","exec","--sandbox","read-only","--skip-git-repo-check","-C","{project_dir}","-o","{lastmsg}"], read_tool: true }   # A read-only pull-model (ADR-01); warm = session.*.advisor
roles:                                                # B - whitelist; RANH GIOI TOOL = local .kiro/agents (P0, muc 15)
  analyzer:     { cmd: ["kiro-cli","chat","--no-interactive","--agent","analyzer"],  edit: false }   # local allowedTools KHONG co shell
  architect:    { cmd: ["kiro-cli","chat","--no-interactive","--agent","architect"], edit: false }   # ten that la "architect" (khong phai architecter)
  reviewer:     { cmd: ["kiro-cli","chat","--no-interactive","--agent","reviewer"],  edit: false }
  coder:        { cmd: ["kiro-cli","chat","--no-interactive","--agent","coder","--trust-all-tools"], edit: true }
  fixer:        { cmd: ["kiro-cli","chat","--no-interactive","--agent","fixer","--trust-all-tools"], edit: true }
  # orchestrator: BO khoi roles (A=Codex la orchestrator). Local override .kiro/agents/orchestrator.json da vo hieu subagent/web (Finding D, muc 15)
```
`roles` là RANH GIỚI BẢO MẬT: harness chỉ spawn đúng argv liệt kê, không nhận chuỗi tự do từ LLM.

### 6.2 Handshake A -> harness (DUYỆT)
Harness cấp 1 `nonce` ngẫu nhiên/lượt trong seed; A kết stdout bằng đúng MỘT directive mang nonce đó (ADR-07).
Parser: đúng MỘT dòng-mở `<<<DISPATCH nonce>>>` (>1 -> turn_error(multiple_directive)); closer = `<<<END nonce>>>`
CUỐI CÙNG (outermost) → body = giữa open và close cuối; 0 directive -> turn_error(missing_directive).
A bị CẤM in lại chuỗi sentinel trong body gửi Kiro (R2-6).
```
<<<DISPATCH nonce="a1b2c3" role="coder" slice="12-K6">>>
<toan bo prompt chuan gui Kiro, nhieu dong>
<<<END nonce="a1b2c3">>>
```
```
<<<PHASE nonce="a1b2c3" id="13" title="..." reconciliation="docs/reports/phase12-final-reconciliation-report.md">>>
```
```
<<<COMPLETE nonce="a1b2c3">>>   # done toan cuc
```
```
<<<VERIFY nonce="a1b2c3" slice="12-K6">>>   # A kich; harness chay verify.gates -> exit code (PASS=tat ca 0) + tail ve A (ADR-04)
```
Role phải thuộc whitelist; thiếu directive / role lạ -> lỗi lượt (không spawn), log, hỏi supervisor/observer.

### 6.3 Handshake Kiro -> harness (DUYỆT)
Kiro tự ghi file `.md` rồi in trailer cuối stdout:
```
<<<REPORT path="docs/reports/phase12-slice-k6-coder-report.md" done="true" verdict="APPROVE">>>
```
`verdict` chỉ có khi role là reviewer/orchestrator (verdict NỘI BỘ). Thiếu trailer -> lượt coi như chưa done, đưa stdout thô cho A.

### 6.4 `conversation.ndjson` (append-only, UTF-8, `\n`)
Chung: `id, ts, type, phase, turn`. Phân biệt theo `type`:
```jsonc
{"id":1,"ts":"...","type":"user_goal","goal":"...","contract_path":"docs/Phase12-plan.md"}
{"id":2,"ts":"...","type":"phase_start","phase":12,"title":"AI Assistant"}
{"id":3,"ts":"...","type":"dispatch","phase":12,"turn":5,"from":"A","to":"kiro:coder","role":"coder","slice":"12-K6","task_id":"task_a1b2","origin":"planned","session_mode":"cold","session_ref_in":null,"session_ref_out":"sess_x9","prompt":"<title/excerpt>","prompt_path":"data/prompts/task_a1b2.md","prompt_sha256":"..."}
{"id":4,"ts":"...","type":"report","phase":12,"turn":5,"from":"kiro:coder","role":"coder","slice":"12-K6","task_id":"task_a1b2","session_ref":"sess_x9","report_path":"docs/reports/phase12-slice-k6-coder-report.md","snapshot_path":"data/reports/0004-phase12-slice-k6-coder.md","sha256":"...","excerpt":"<~500 ky tu>","done":true,"verdict":null}
{"id":4b,"ts":"...","type":"session_end","task_id":"task_a1b2","session_ref":"sess_x9","reason":"task_done"}
{"id":5,"ts":"...","type":"gov","phase":12,"turn":5,"from":"supervisor","verdict":"continue","reason":"bam contract S12"}
{"id":6,"ts":"...","type":"verify","phase":12,"turn":6,"slice":"12-K6","exit":0,"failed_gate":null,"tail":"<~500 ky tu cuoi>"}
{"id":7,"ts":"...","type":"slice_done","phase":12,"slice":"12-K6","commit":"<git sha>"}
{"id":8,"ts":"...","type":"turn_error","phase":12,"turn":7,"reason":"missing_directive","detail":"A khong phat directive hop le"}
{"id":9,"ts":"...","type":"phase_end","phase":12,"reconciliation_path":"docs/reports/phase12-final-reconciliation-report.md"}
```
QĐ thiết kế (CẬP NHẬT): prompt đầy đủ của role-agent được materialize vào `data_dir/prompts/<task_id>.md`; `dispatch` lưu `prompt_path` + `prompt_sha256` và có thể lưu `prompt` dạng title/excerpt để UI dễ đọc. `report` lưu path + excerpt + `snapshot_path` (bản sao .md trong data_dir) + `sha256` → audit/replay self-contained (ADR-05). Thêm type `verify` (kết quả build/test, gắn slice), `slice_done` (APPROVE+verify_ok → git commit, lưu `commit` map snapshot↔commit, ADR-02), `turn_error`, `resume` (ADR-08).

### 6.5 `status.json` (ghi đè mỗi lượt — cây cho GUI)
```jsonc
{
  "schema":1, "state":"running",
  "current":{"phase":12,"slice":"12-K6","role":"coder","turn":34},
  "metrics":{"turns":34,"started_at":"...","updated_at":"..."},
  "plan":[
    {"phase":12,"title":"AI Assistant","state":"doing",
     "slices":[
       {"id":"12-K6","title":"Inbox sync","state":"doing","verdict":"pending",
        "tasks":[{"id":"t1","desc":"sync worker","state":"done"},
                 {"id":"t2","desc":"reply executor","state":"doing"}]}
     ]}
  ]
}
```
State node: `todo|doing|done|blocked|rejected`. Verdict slice: `pending|APPROVE|REJECT`.
(v1 có thể chỉ track phase/slice/role/turn/verdict; cây task chi tiết là tùy chọn.)

### 6.6 `control.json` (observer) + `decisions.ndjson` (governance) + luật ưu tiên
```jsonc
// control.json - CHI observer ghi; harness doc o ranh gioi luot
{"seq":3,"ts":"...","verdict":"steer","inject":"uu tien fix R1 truoc"}   // continue|pause|steer|stop

// decisions.ndjson - append-only: supervisor + su kien mau thuan
{"id":12,"ts":"...","actor":"supervisor","verdict":"steer","reason":"...","overrides_observer":false}
{"id":13,"ts":"...","actor":"harness","event":"conflict","winner":"supervisor","observer_seq":3,"note":"observer=continue nhung supervisor=stop"}
```
Luật ranh giới lượt: đọc cả control.json (so `seq` để biết lệnh mới) lẫn verdict supervisor;
xung đột -> supervisor thắng + ghi `conflict`. `pause` -> chạy hết lượt rồi chờ `seq` mới.

---

## 7. Thiết kế module

### 7.1 Bản đồ thư mục
```
parley/
  harness.py     # loop tat dinh + guardrails + kill switch (trai tim); dung fsm.py
  fsm.py         # verdict-gate free|strict + slice_done idempotent (ADR-03/R2-1/R2-9)
  config.py      # Config typed (Limits/Verify/Role); cli doc/ghi parley.config.json (v1 JSON, khong can pyyaml)
  agents.py      # DEPRECATED (ADR-10) -> dung backends.py
  backends.py    # AgentBackend.run_once->RunResult + VerifyRunner (ADR-10); GenericCliBackend cho codex+kiro
  supervisor.py  # gate: doc goal+contract -> verdict (LLM judge; KHONG spawn)
  context.py     # seed nhe cho A (pull-model) + input tu-du-nghia cho B
  protocol.py    # parse/emit <<<DISPATCH>>> <<<REPORT>>> <<<COMPLETE>>> <<<PHASE>>> <<<VERIFY>>>
  channel.py     # ghi/doc conversation.ndjson, status.json, control.json, decisions.ndjson + snapshot report
  gitio.py       # auto-commit moi slice (rollback R1, ADR-02): commit_slice -> sha
  cli.py         # `parley init <folder> --goal` (muc 16) + `parley run`
  store.py       # GUI nac A: registry projects/goals + config/data per-goal (muc 18)
  manager.py     # GUI nac A: start/stop goal lam tien trinh `parley run` (muc 18)
  web/app.py     # FastAPI: /feed (SSE), /status, /control, / (bearer token) -- can `pip install fastapi uvicorn`
  web/index.html
  parley.config.yaml
  data/
```

### 7.2 Harness loop (giả mã — toàn bộ dataflow)
```
cfg, goal, contract = load()
st = channel.resume()                                   # ADR-08: dung lai tu ndjson neu co
if st: phase, turn, fsm.state = st.phase, st.turn, st.fsm
       if st.pending_dispatch: state = "paused"; channel.emit("resume", note="dispatch chua co report -> cho nguoi")
else:  channel.emit("user_goal", goal=..., contract_path=...)
       phase = first_phase(); channel.emit("phase_start", phase)
a_ctx = context.advisor_seed(goal, contract, phase, prev_reconciliation=None)

def maybe_commit(slice):                                # R2-9: kiem sau MOI doi trang thai FSM; slice_done True dung 1 lan
    if fsm.slice_done(slice):
        sha = gitio.commit_slice(slice); channel.emit("slice_done", slice=slice, commit=sha)

iters = 0; consec_errors = 0
while state == running and turn < cfg.max_turns:
    iters += 1
    if iters > cfg.limits.hard_iter_cap: stop("stuck"); break   # R2-10: chan A spin du turn dung yen

    ctl = channel.read_control()                        # R2-8: doc o DAU vong (pause/stop TRUOC khi goi A -> khong phi compute)
    if ctl and ctl.verdict == stop:  stop("observer"); break
    if ctl and ctl.verdict == pause: wait_until_control_changes(); continue

    nonce = new_nonce()                                 # ADR-07: cap moi luot, nhet vao seed A
    a_out = agents.run_advisor(a_ctx, nonce)            # A (pull-model: tu doc report qua read_tool) -> directive cuoi
    d     = protocol.parse(a_out, nonce)                # DISPATCH | COMPLETE | PHASE | VERIFY | NONE | MULTIPLE
    if d.kind in (NONE, MULTIPLE):
        consec_errors += 1
        if consec_errors > cfg.limits.max_turn_errors: stop("stuck"); break   # R2-10: chuoi directive loi
        channel.emit("turn_error", reason=d.kind); a_ctx = context.advisor_reject(d.kind); continue
    consec_errors = 0
    if d.kind == COMPLETE: stop("done"); break
    if d.kind == VERIFY:                                # R2-1: gan ket qua verify vao slice + fsm
        v = agents.run_verify()                         # chay het verify.gates; {code, failed_gate, tail}; timeout rieng
        channel.emit("verify", slice=d.slice, exit=v.code, failed_gate=v.failed_gate, tail=v.tail)
        fsm.observe_verify(d.slice, v.code)             # cap nhat verify_ok cho slice
        maybe_commit(d.slice)                           # R2-9: verify co the la buoc chot slice
        a_ctx = context.advisor_verify(v); continue
    if d.kind == PHASE:                                 # ranh gioi Phase -> reset session A
        channel.emit("phase_end", reconciliation_path=d.reconciliation)
        phase = d.phase; channel.emit("phase_start", phase)
        a_ctx = context.advisor_seed(goal, contract, phase, prev_reconciliation=d.reconciliation)
        continue

    if not fsm.allow(d, mode=cfg.pipeline.mode):        # ADR-03: strict=reject, free=log; chuyen slice can APPROVE + verify_ok
        channel.log_decision(actor="harness", event="deviation", note=fsm.why)
        if cfg.pipeline.mode == strict: a_ctx = context.advisor_reject(fsm.why); continue

    gate_due = (turn % cfg.supervisor.every_n_turns == 0) or cfg.roles[d.role].edit   # R2-11: edit:true LUON gate
    dec = supervisor.gate(goal, contract, intent=d, recent=channel.tail()) if gate_due else CONTINUE
    dec = merge_control(dec, ctl)                       # supervisor thang; log conflict; steer inject
    if dec.verdict == stop:  stop("gov"); break
    if dec.verdict == steer: d.prompt += "\n\n[STEER] " + dec.inject

    channel.emit("dispatch", role=d.role, slice=d.slice, prompt=d.prompt)
    channel.emit("gov", verdict=dec.verdict, reason=dec.reason)

    b_in  = context.executor_input(d, contract)         # tu du nghia
    b_out = agents.run_role(d.role, b_in)               # spawn whitelist + idle/hard timeout + kill
    rep   = protocol.parse_report(b_out)                # path, done, verdict?
    snap  = channel.snapshot_report(rep.path)           # copy .md -> data_dir + sha256 (ADR-05)

    channel.emit("report", role=d.role, slice=d.slice, report_path=rep.path,
                 snapshot_path=snap.path, sha256=snap.sha,
                 excerpt=rep.excerpt, done=rep.done, verdict=rep.verdict)
    channel.update_status(phase, d.slice, d.role, turn, rep.verdict)
    fsm.observe(rep)                                    # cap nhat APPROVE/REJECT cho slice
    maybe_commit(d.slice)                               # R2-9: report APPROVE co the la buoc chot slice

    if loop_detected(): stop("loop"); break             # exact match HOAC role-cycle lap tren cung slice
    a_ctx = context.advisor_followup(rep)               # pull-model: chi day pointer+excerpt, A tu doc full
    turn += 1
```

### 7.3 Seam (chữ ký module)
```python
# agents.py - adapter tat dinh, prompt ngan qua STDIN + PROMPT_PATH, KHONG qua argv
def run_role(role: str, prompt: str) -> str: ...      # spawn cfg.roles[role].cmd; reset idle khi co output; kill khi idle>idle_timeout hoac total>hard_timeout
def run_advisor(ctx: str, nonce: str) -> str: ...     # A (codex exec, read_tool=true); nonce nhet vao seed (ADR-07)
def run_verify() -> Verify: ...                       # chay TUAN TU cfg.verify.gates; {code, failed_gate, tail}; PASS=tat ca 0; timeout=cfg.verify.timeout_s (ADR-04/09/R2-4)

# supervisor.py - LLM judge, doc goal+contract MOI luot (FR4)
def gate(goal, contract, intent, recent) -> Decision: ...   # {verdict, reason, inject}; parse JSON phong thu

# harness.py - fsm verdict-gate (free|strict, ADR-03); state per-slice = {approve, verify_ok}
def allow(directive, mode) -> bool: ...               # strict: REJECT->fixer cung slice; chuyen slice CAN approve AND verify_ok (R2-1)
def observe(report): ...                              # cap nhat approve/reject cho slice tu verdict
def observe_verify(slice, exit_code): ...             # cap nhat verify_ok cho slice (R2-1)
def slice_done(slice) -> bool: ...                    # True LAN DAU khi approve AND verify_ok (sau do False) -> commit dung 1 lan (R2-9)

# gitio.py - auto-commit/slice (R1 rollback, ADR-02)
def commit_slice(slice) -> sha: ...                   # git add -A && commit "parley: slice <slice>"; tra hash de map vao slice_done

# context.py - seed nhe (pull-model, ADR-01): A tu doc full report qua read_tool
def advisor_seed(goal, contract, phase, prev_reconciliation) -> str: ...   # dau Phase: goal+contract+reconciliation; contract=null -> seed bao A bat dau bang dispatch analyzer/architect tao contract (R2-7)
def advisor_followup(report) -> str: ...                                   # day pointer(path)+excerpt, A tu pull full
def advisor_verify(verify) -> str: ...                                     # day exit code + tail ve A
def advisor_reject(why) -> str: ...                                        # bao A vi pham fsm / thieu directive
def executor_input(directive, contract) -> str: ...                        # prompt + tro contract_path/project_dir

# protocol.py - handshake LLM<->harness
def parse(advisor_stdout, nonce) -> Directive: ...    # DISPATCH(role,slice,prompt)|PHASE(id,recon)|VERIFY(slice)|COMPLETE; closer=END cuoi; 0->NONE, >1 open->MULTIPLE (ADR-07/R2-6)
def parse_report(executor_stdout) -> Report: ...      # path, excerpt, done, verdict

# channel.py - so huu 4 file du lieu (ghi atomic: temp+rename cho status/control)
def emit(type, **kw): ...                # append conversation.ndjson (1 write tron dong + \n)
def update_status(...): ...              # ghi de status.json atomic (cay plan)
def snapshot_report(path) -> Snap: ...   # copy .md -> data_dir/reports + sha256 (ADR-05)
def read_control() -> Control | None: ...# doc control.json, so seq
def resume() -> State | None: ...        # dung lai phase/turn/fsm tu ndjson; dispatch chua co report -> paused (ADR-08)
def log_decision(**kw): ...              # append decisions.ndjson (gov + conflict + deviation)
def tail(n=...) -> list: ...
```

---

## 8. Quyết định thiết kế đã duyệt
1. Cú pháp directive `<<<DISPATCH>>>` / `<<<REPORT>>>` / `<<<PHASE>>>` / `<<<COMPLETE>>>` (thay vì JSON thuần) — dễ tách khỏi văn bản LLM.
2. Report lưu path + excerpt trong ndjson (không nhồi full text).
3. Tách `control.json` (observer) và `decisions.ndjson` (governance).
4. **A pull-model + warm session trong Phase** (ADR-01 + ADR-14): lượt đầu Phase seed goal+contract+reconciliation;
   lượt kế `run_turn` resume (headless, KHÔNG TTY attach). A KHÔNG bị nhồi full report; A dùng file-read tool
   tự đọc report theo path khi cần → context bounded. A vẫn read-only (không edit/shell).
5. **Mode FSM bật/tắt** (ADR-03): `pipeline.mode=free|strict`. FSM verdict-gate nằm ở harness cả 2 mode;
   free = chỉ log `deviation`, strict = reject directive vi phạm. Invariant strict (tối thiểu):
   REJECT→fixer cùng slice; **chuyển slice cần slice hiện tại có APPROVE VÀ verify_ok** (R2-1). KHÔNG ép full thứ tự role.
6. **Verify do A kích, harness chạy** (ADR-04): A phát `<<<VERIFY slice=...>>>`; harness chạy `verify.gates`
   (nhiều cổng, PASS=tất cả exit 0) → ground truth tất định, nạp vào `fsm.observe_verify`. "Done" slice = APPROVE + verify_ok.
7. **Snapshot report vào data_dir** (ADR-05): emit `report` kèm copy .md + `sha256` → replay self-contained.

---

## 9. Guardrails & bảo mật
- Kill switch + idle/hard timeout: harness/agents sở hữu, KHÔNG phụ thuộc LLM.
- Whitelist command: chỉ spawn `cfg.roles[role].cmd`.
- Stop: COMPLETE | MAX_TURNS | hard_iter_cap/chuoi turn_error ("stuck", R2-10) | loop (exact match HOAC role-cycle lap tren cung slice) | supervisor stop | observer stop.
- **Gate-cho-edit (R2-11, BẤT BIẾN)**: mọi intent `edit:true` LUÔN bị supervisor gate bất kể `every_n_turns`; chỉ role read-only mới được bỏ qua theo nhịp. Gate là chốt an toàn ngữ nghĩa cuối trước khi `--trust-all-tools` chạm code thật.
- Web: bind 127.0.0.1 + bearer token MỌI request (bắt buộc cho /control); ra ngoài qua tunnel có danh tính (Tailscale/Cloudflare/SSH). Không log lộ secret.
- Orchestrator/web tách tiến trình (web sập, agent vẫn chạy).
- A có file-read tool (read-only, pull-model) + kích `<<<VERIFY slice>>>`; harness chạy `verify.gates` (whitelist). A KHÔNG có shell tự do, KHÔNG edit.
- **CẢNH BÁO R1 (ADR-02 — rủi ro CHẤP NHẬN có ý thức)**: chọn git-branch (KHÔNG OS-sandbox). Role `edit:true`
  chạy `--trust-all-tools` trên codebase THẬT. Bắt buộc: nhánh git riêng + auto-commit mỗi slice (rollback được)
  + KHÔNG trỏ `project_dir` vào production + giám sát vài vòng trước khi chạy đêm.
  RỦI RO TỒN DƯ: git-branch KHÔNG chặn ghi ngoài repo / network / đọc secret. Hardening OS để dành mở sau.

---

## 10. Lộ trình build
- **P0 spike**: verify `codex exec` (stdin/in hết/exit 0 + **có file-read tool** cho pull-model ADR-01) và `kiro-cli chat --agent ...`. Chốt tunnel + model supervisor.
- **P1**: protocol.py + agents.py + channel.py + harness loop **chỉ role read-only** (analyzer/architect/reviewer). Chạy tay, giám sát. (CHƯA bật edit-role.)
- **P2**: supervisor.gate + steer/stop + context theo Phase (seed nhẹ) + fsm verdict-gate (mode free).
- **P3**: web /feed (SSE) + /status + index.html (read-only).
- **P4**: /control + control.json tại ranh giới lượt + bearer token + tunnel.
- **P5**: git-branch sandbox + auto-commit/slice (ADR-02) + `<<<VERIFY>>>` (ADR-04) + loop-detect (exact + role-cycle) → **chỉ sau đây mới bật edit-role**, chạy đêm có log.

---

## 11. Câu hỏi mở (Phase 0) — ĐÃ XÁC MINH 2026-06-01 (chi tiết mục 15)
- [x] `codex exec` đọc stdin, exit 0, có read-only sandbox → đúng ADR-01. (`-o` ghi last message để parse sạch.)
- [x] `kiro-cli chat --no-interactive --agent <role>` (+ `--trust-all-tools`) đọc stdin, exit 0.
- [x] Cơ chế Kiro ghi report đúng path → nhúng vào system-prompt agent local `.kiro/agents/*.json`.
- [ ] Kênh tunnel: Tailscale / Cloudflare Tunnel / SSH? (chưa chốt)
- [ ] Model cho supervisor + model cho A (codex `--model`). (chưa chốt)
- [ ] Micro-test còn lại: trong `--no-interactive`, tool KHÔNG nằm trong allowedTools bị deny hay treo? `--trust-tools` ghi đè hay hợp nhất allowedTools? (kiểm khi dựng agents.py)

---

## 12. Checklist cho Architect review
> Đã review + chốt (2026-06-01). Toàn bộ quyết định tại mục 13 (ADR-01..09). Tất cả mục đã đóng [x].
- [x] Mô hình SPARC bất đối xứng (mục 1) có đúng ý đồ vận hành không? → ĐÚNG, giữ.
- [x] Tách "LLM-supervisor (phán xét)" vs "code spawn" (2.1) → ADR-06: harness sở hữu spawn, supervisor thuần judge.
- [x] 6 hợp đồng dữ liệu (mục 6) đã đủ field chưa? → bổ sung `verify`/`turn_error`/`resume`/snapshot + state enum (6.4/6.5).
- [x] Handshake directive (6.2/6.3) có đủ chặt không? → ADR-07: nonce-fence + block cuối + đúng-một.
- [x] QĐ A trong Phase (8.4) → ADR-01 pull-model.
- [x] Report đủ cho replay/audit chưa? → ADR-05 snapshot + sha256.
- [x] Guardrails che R1/R2/R3? → R2 đủ; R1 ADR-02 (rủi ro tồn dư đã ghi); R3 thêm role-cycle detect.
- [x] Lộ trình P0-P5 hợp lý? → reorder: edit-role chỉ bật ở P5 sau gate+sandbox.

---

## 13. Architect Decision Records (chốt 2026-06-01)

### ADR-01 — Context của A: pull-model (A có file-read tool)
- **Quyết định**: A là viewer + có file-read tool read-only. Seed mỗi lượt chỉ gồm goal + contract Phase
  + reconciliation Phase trước + progress-log gọn; A tự đọc full report theo path khi cần.
- **Lý do**: nhồi full report vào context A sẽ tràn context window trên Phase nhiều slice (a..k). Pull-model
  giữ context bounded mà A vẫn xem được report nguyên văn.
- **Hệ quả**: `context.advisor_seed/followup` chỉ truyền pointer+excerpt. P0 phải verify `codex exec` có file-read.
  A đọc trong `project_dir` (read-only) — không tăng quyền edit.
- **Contract là artifact neo (R2-7)**: Kiro architect tạo/cập nhật `contract_path`; A đọc bản mới nhất mỗi lượt.
  Bootstrap đầu Phase khi `contract_path=null` → seed cho phép, A bắt đầu bằng dispatch analyzer/architect để tạo contract.

### ADR-02 — Sandbox R1: git-branch (KHÔNG OS-sandbox) — rủi ro chấp nhận
- **Quyết định**: dùng nhánh git riêng + auto-commit mỗi slice (owner: `gitio.commit_slice`, R2-2); không đầu tư OS-sandbox ở v1.
  Commit hash ghi vào event `slice_done` để map snapshot↔commit.
- **Lý do**: công cụ cá nhân, chạy trên máy mình, target là repo của mình; ưu tiên tiến độ.
- **Hệ quả / RỦI RO TỒN DƯ**: git-branch KHÔNG chặn ghi ngoài repo, network, hay đọc secret khi role
  `--trust-all-tools` chạy đêm. Bắt buộc: không trỏ production + giám sát vài vòng trước khi để qua đêm.
  OS-sandbox để dành mở sau nếu nâng mức tự động.

### ADR-03 — State machine: bật/tắt free|strict
- **Quyết định**: `pipeline.mode=free|strict`. FSM verdict-gate ở harness cả hai mode; free chỉ log
  `deviation`, strict reject directive vi phạm.
- **Lý do**: cho thời gian đánh giá A có bám SPARC không (free) trước khi siết (strict).
- **Hệ quả**: invariant strict (REJECT→fixer cùng slice; chuyển slice cần APPROVE **VÀ** verify_ok — R2-1).
  KHÔNG ép full thứ tự role vì chuỗi role chưa xác nhận chắc chắn.

### ADR-04 — Build/test: A kích `<<<VERIFY slice>>>`, harness chạy đa-cổng, nạp vào FSM
- **Quyết định**: A quyết khi nào verify + diễn giải kết quả (độc lập lời khai B = công bằng); harness chạy
  `verify.gates` (nhiều cổng, PASS=tất cả exit 0) → ground truth tất định, `fsm.observe_verify(slice, code)`.
  "Done" slice = APPROVE **VÀ** verify_ok (FSM thực thi, R2-1). VERIFY directive mang `slice` để gắn kết quả.
- **Lý do**: vừa thỏa "A làm build/test", vừa không cấp shell tự do cho A (giữ read-only), vừa loại bỏ
  "done do LLM tự nhận"; verify_ok được FSM dùng nên định nghĩa done không bị rỗng.
- **Hệ quả**: directive `<<<VERIFY slice>>>` + `agents.run_verify()` (đa-cổng, R2-4) + event `verify`(slice) + `fsm.observe_verify`.

### ADR-05 — Snapshot report vào data_dir
- **Quyết định**: khi emit `report`, copy .md vào `data_dir/reports` + lưu `sha256`.
- **Lý do**: report sống ở repo đích (có thể bị sửa/xóa/rebase) → replay/audit không tin cậy. Snapshot làm
  `data_dir` self-contained, đúng tiêu chí PRD §9.
- **Hệ quả**: thêm `channel.snapshot_report()`; ndjson `report` có `snapshot_path` + `sha256`.

### ADR-06 — Spawn ownership: harness sở hữu spawn, supervisor thuần LLM-judge
- **Quyết định**: harness là chủ DUY NHẤT của subprocess (spawn whitelist + kill switch + timeout).
  `supervisor.gate` chỉ trả verdict; approve = "nút cấp phép" cho harness spawn. KHÔNG có lớp code supervisor riêng.
- **Lý do**: gom điểm chạm subprocess vào một nơi tất định, trust boundary rõ, hết mâu thuẫn 2.1 vs 7.2.
- **Hệ quả**: 2.1 + bảng mục 2 viết lại; vẫn honor intent "supervisor bấm nút chạy" (= approve).

### ADR-07 — Directive parsing: nonce-fence + closer cuối + đúng-một (gồm refinement R2-6)
- **Quyết định**: harness cấp `nonce` ngẫu nhiên/lượt vào seed A; directive phải mang nonce. Parser: đúng MỘT dòng-mở
  `<<<DISPATCH nonce>>>` (>1 → `turn_error(multiple_directive)`); closer = `<<<END nonce>>>` CUỐI CÙNG (outermost);
  0 directive → `turn_error(missing_directive)`. A bị CẤM in lại sentinel trong body gửi Kiro (R2-6).
- **Lý do**: nonce tươi + outermost-close + "đúng một" chống va chạm sentinel/echo/footer → không cắt prompt sớm.
- **Hệ quả**: `protocol.parse(stdout, nonce)`; `agents.run_advisor(ctx, nonce)` nhét nonce + nhắc cú pháp.

### ADR-08 — Crash recovery: resume từ ndjson, safe-by-default
- **Quyết định**: ndjson là nguồn sự thật, status.json dựng lại được. Khởi động: `channel.resume()` replay khôi phục
  phase/turn/fsm. Nếu sự kiện cuối là `dispatch` chưa có `report` (edit-role có thể chạy nửa chừng) → `paused`
  + emit `resume`, để observer/git-diff quyết, KHÔNG tự chạy tiếp.
- **Lý do**: run đêm phải bền với crash; auto-continue một edit dở dang quá rủi ro.
- **Hệ quả**: thêm `channel.resume()` + event `resume`; ADR-02 auto-commit/slice giúp git-diff soi edit dở.

### ADR-09 — Timeout verify/build riêng
- **Quyết định**: `verify.timeout_s` (mặc định 600) tách khỏi `idle_timeout_s` của role; cho phép override idle
  per-role cho edit-role chạy build dài.
- **Lý do**: build/test im stdout lâu hợp lệ; idle 120s sẽ giết oan.
- **Hệ quả**: verify dùng timeout riêng (`VerifyRunner`, ADR-10).

### ADR-10 — Backend abstraction + ranh giới session/attach (chốt 2026-06-07)
- **Quyết định**: tách transport khỏi harness. Harness CHỈ phụ thuộc `AgentBackend.run_once(profile, prompt, cwd, idle, hard) -> RunResult` (và `SessionBackend.run_turn -> RunResult`), cùng `VerifyRunner.run(gates, cwd, timeout) -> Verify` tách riêng (verify là governance, không phải transport).
- **`RunResult`** = `{stdout, exit_code, timed_out, session_ref}`. Harness vẫn parse trailer từ `stdout`; `session_ref` chỉ cho audit/resume, KHÔNG ảnh hưởng FSM.
- **Phân lớp**: `AgentBackend → GenericCliBackend` (Codex/Kiro profile, stdin/stdout one-shot). `SessionBackend` (opt-in) → `HeadlessResumeBackend` (codex exec resume — sạch, không TTY) + `InteractiveAttachBackend` (experimental, TTY/tmux). SessionBackend phơi `run_turn()` cấp cao; `send()/wait()` KHÔNG được rò vào harness. `VerifyRunner → SubprocessVerifyRunner`.
- **Bất biến khoá**: (1) harness chỉ dựa `run_once/run_turn -> RunResult`; (2) trailer `<<<REPORT ...>>>` luôn bắt buộc; (3) verify exit code tất định & độc lập backend; (4) headless-resume optional, `session_ref` PHẢI ghi vào audit; (5) interactive-attach là experimental, KHÔNG được tuyên bố có deterministic kill / complete audit / reliable resume; (6) session backend KHÔNG được trực tiếp đổi FSM/governance state.
- **Lý do**: mượn kiến trúc adapter/transport của ai-devkit để đa-provider, mà KHÔNG đánh mất governance/audit/kill switch tất định — giá trị riêng của Parley.
- **Hệ quả**: seam `agents.*` ở mục 7.3 được thay bởi `backends.*`; `agents.py` deprecated. Chi tiết cold/warm
  theo task → **ADR-14**.

### ADR-11 — Advisor review first-class + commit/push ownership (hybrid) (chốt 2026-06-07)
- **advisor_review BẮT BUỘC sau mỗi report**: A nêu `APPROVE|REJECT` + `note` (đề xuất) qua thuộc tính `review=`/`note=` trên directive kế. Event mới `advisor_review` (first-class). Chuỗi audit: `report → advisor_review → gov(supervisor) → dispatch → task → report`. Đổi thứ tự emit `gov` TRƯỚC `dispatch`.
- **Approver = hybrid**: `advisor_review=APPROVE` (+ verify exit 0 NẾU diff chạm code) = cổng commit cho slice. Slice read-only (chỉ ghi report) → commit khi APPROVE, không cần verify. `REJECT` → dispatch fixer. `reviewer`-role là lớp review độc lập TÙY CHỌN (A tự gọi / `strict` ép).
- **Diff**: sau edit-role report, harness chạy `git diff` (project_dir) → ghi bằng chứng + feed cho A để advisor_review dựa trên thay đổi THẬT (không phải self-claim).
- **Commit**: HARNESS thực thi `git commit` (tất định), message do `agent-git` soạn; **per approved slice** trên `branch=parley/work`; cấm agent reset/force/amend; cấm chạm branch protected (main/master/develop).
- **Push**: opt-in (`git.auto_push`), CHỈ khi goal kết thúc THÀNH CÔNG (`stop_reason="done"`); work branch, never main/master, never force. Có thể thêm nút "push now" cho observer.

### ADR-12 — Side-channel housekeeping (agent-document + agent-git) (chốt 2026-06-07)
- Trên approve (advisor_review APPROVE [+verify]) → enqueue "record job" Ở NGOÀI slice FSM (KHÔNG gate slice_done/next-slice). `agent-document` cập nhật docs/changelog; commit message do `agent-git`; HARNESS thực thi git.
- **An toàn (hazard đa-writer cùng git working tree)**: `project_dir` write-lock dùng chung giữa edit-slice (coder/fixer) và housekeeping. Read-only roles + advisor reasoning + supervisor KHÔNG cần lock → chạy song song, goal vẫn tiến. Chỉ writer serialize. (`git worktree` riêng cho housekeeping = tối ưu tương lai.)
- "Không ảnh hưởng slice" = không chặn dòng quyết định/planning; chỉ edit kế phải nhường lock vài giây lúc đang commit.

### ADR-13 — Init/Contract/Plan + Task entity + execution mode (chốt 2026-06-07)
Phân cấp: **Project → Init → Goals → Tasks → Events**. UI 4 cột: Projects | Goals(Contract) | Conversation | Task List.

- **Init (2 phần)**: (a) deterministic setup (detect stack, sync `.kiro/agents`, branch) — 1 lần/project, prereq; (b) **planner** (analyzer/architect) đọc ideas/features (nút "+" ở dòng Init) → sinh goal-list + `execution_mode` + `reason` → user review/approve. Lưu project-level: `{"execution_mode":"sequential|parallel","goals":[...],"reason":"..."}`.
- **Contract = goal-list cấp project** (cột Goals) với done-state: ☑done(stop_reason=done) ◐running ○todo ✗stopped. = kế hoạch/chiến lược bền.
- **Plan → Goals UX**: Advisor/chat chỉ tạo **draft contract**. End-user có editor `execution_mode`, `reason`,
  và goal-list một dòng/goal; sau review phải bấm `Approve & Replace` hoặc `Approve & Append` mới tạo goal thật.
  Replace supersede goal cũ chưa chạy nhưng giữ registry/data để audit; Append nối goal mới vào contract hiện tại.
- **Advisor artifact UX**: Init Advisor vẫn read-only. Khi cần tài liệu như `domain-contract.md`, Advisor soạn nội
  dung hoàn chỉnh trong chat; end-user dùng Artifact Draft (`last advisor → artifact`, chỉnh nội dung/path, `save artifact`)
  để Parley backend ghi file text vào `project_dir` với path guard. Advisor không tự ghi file.
- **Task = PROJECTION trên event log** (KHÔNG store thứ 2). Mỗi dispatch mang `task_id` + `origin`(planned|emergent) + `parent_task_id`. Task-list dựng từ events → `conversation.ndjson` vẫn là nguồn sự thật duy nhất (audit/replay/resume nguyên vẹn). Emergent (fixer/reviewer phát sinh) = dispatch event mới `origin=emergent` + `parent_task_id` (link task gốc) → tự append. `slice` giữ cho FSM commit-gate.
- **Task status**: planned | queued | running | awaiting_advisor_review | rejected | verifying | done | blocked.
- **execution_mode**: `sequential` mặc định (an toàn). `parallel` chạy nhiều goal đồng thời trên CÙNG repo = hazard working-tree → BẮT BUỘC `git worktree` riêng mỗi goal; hoãn tới khi có worktree (S4). **v1 = sequential thuần: commit done → next task; KHÔNG parallel/lock (B2 đã bỏ).**
- **UI cột 4 (Task List)**: task của goal đang chọn (planned + emergent append realtime); click task → filter Conversation theo `task_id`.

Build sequence: S1 task-projection (emit task_id/origin/parent + endpoint `/goals/{id}/tasks`) → S2 UI cột Task List + filter → S3 Init split + planner + approve → S4 sequential runner (parallel+worktree sau).

### ADR-14 — Task-scoped session policy: warm-until-done (chốt 2026-06-07)

**Vấn đề**: Plan cũ ghi “B stateless mỗi spawn” — đúng cho audit/kill switch nhưng lãng phí token khi
Advisor **REJECT** / chỉnh plan (analyzer/architect), fix nhẹ (fixer), hay re-review (reviewer): cold spawn buộc
agent làm lại từ đầu dù chỉ cần delta. Policy không nên gắn cứng vào từng role — **mọi agent executor** đều
có thể cần vòng chỉnh sửa trên cùng task.

- **Quyết định**: mỗi **`task_id`** (ADR-13) gắn tối đa **một `session_ref` active**. Harness quyết định
  cold/warm **tất định** (không để LLM tự chọn transport).
- **Lần đầu** dispatch task → `session_mode=cold` → `run_once` → lưu `session_ref_out` trên event `dispatch`/`report`.
- **Chỉnh sửa cùng task** (task chưa `done`) → `session_mode=warm` → `run_turn(session_ref_in, delta_prompt)`;
  delta = reject note / steer / yêu cầu chỉnh plan — **KHÔNG** gửi lại toàn bộ brief cold trừ khi force cold.
- **Task `done`** (advisor APPROVE + slice gate thỏa, hoặc task projection chuyển `done`) → emit `session_end`
  → session vô hiệu → dispatch kế **bắt buộc cold** (task_id mới hoặc cùng slice role khác).
- **Force cold** (bất kể session còn): đổi `slice` | `phase` | `role` | vượt `max_warm_turns_per_task` |
  resume lỗi/timeout | observer/supervisor stop.

| Agent | Policy | Reset khi |
|-------|--------|-----------|
| **Advisor (A)** | `warm_per_phase` | `<<<PHASE>>>` / goal mới / force cold |
| **Init Advisor** (chat/planner) | `warm_per_project` | approve contract / project mới |
| **Supervisor** | `cold` | mỗi gate (ngắn, độc lập) |
| **Mọi role B** (analyzer, architect, reviewer, coder, fixer) | `warm_until_task_done` | `session_end` / force cold |

**Ví dụ architect chỉnh plan**: architect dispatch → report → Advisor **REJECT** “thiếu mục API contract” →
harness **resume cùng `task_id` warm** với delta (note reject + diff nếu có) — architect cập nhật report/plan,
**không** cold respawn + prompt khổng lồ từ A. A (Advisor) cũng warm trong Phase nên không re-seed full header
mỗi lượt. Role khác (REJECT → dispatch **fixer**): **task_id mới** (`origin=emergent`, `parent_task_id`) —
cold lần đầu; fixer có thể warm trên **task fixer đó** nếu REJECT lặp.

**Orchestrator**: Parley **không** dispatch role `orchestrator` Kiro (A = Codex). Mọi quy tắc trên áp dụng
analyzer/architect/reviewer/coder/fixer + Advisor Init.

- **Audit**: `dispatch` mang `task_id`, `session_mode`, `session_ref_in|out`; `report` mang `session_ref`;
  `session_end` mang `reason` (`task_done|forced_cold|max_turns|error`). Replay: cold = đọc full prompt trong log;
  warm = cần backend hỗ trợ resume headless (ADR-10 `HeadlessResumeBackend`).
- **Bất biến**: (1) FSM/governance **không** đọc session state — chỉ task_id + events; (2) warm **không** thay
  supervisor gate / advisor_review / verify; (3) interactive-attach vẫn experimental; (4) prompt cold vẫn phải
  tự đủ nghĩa (fallback khi resume fail → force cold + retry một lần).

**Trạng thái hiện tại (2026-06-07)**: v1 harness vẫn `run_once` mọi lượt; `session_ref` field có trên `RunResult`
/report nhưng **chưa** implement ADR-14 — thuộc P1 backlog §19.3.

**Trạng thái 2026-06-09 (IMPLEMENTED — advisor + supervisor warm-per-phase)**: harness giờ giữ warm session
cho **advisor** (primary codex) và **supervisor** (primary kiro) **trong cùng một phase**, thay vì cold mọi lượt.
- **Advisor**: turn đầu phase = cold `start_cmd` (`codex exec --json --sandbox read-only`) + full seed
  (`advisor_seed`); turn 2+ = warm `resume_cmd` (`codex exec resume --json -c sandbox_mode=read-only {session_id}`)
  + **delta prompt** (`context.advisor_followup_delta/verify_delta/reject_delta` — bỏ header/policy/proto, chỉ giữ
  nhắc nonce). `session_id` = `thread_id` trích từ JSONL (`advisorchat.parse_jsonl`); warm codex output được
  **unwrap** JSONL → reply (newline thật) trước khi `protocol.parse` (nếu không, directive bị escape → NONE).
- **Supervisor**: warm qua `kiro-cli --resume-id {session_id}` (id học bằng **`--list-sessions` diff** — kiro KHÔNG
  in id ở headless; regex `Chat SessionId: <uuid>` sau strip ANSI, KHÔNG json.loads vì `--format json` vẫn trả plain;
  diff = 1 id → dùng, ambiguous 0/>1 → cold). **MẶC ĐỊNH TẮT** (`supervisor_warm=False`, chỉnh 2026-06-09): list-diff
  in-memory không an toàn đa-tiến-trình (mỗi goal = process riêng; 2 goal cùng cwd diff nhầm nhau), và supervisor
  prompt vốn ngắn nên lợi ích token thấp. Bật lại cần file-lock theo cwd (chưa làm). Code warm supervisor giữ nguyên,
  chỉ tắt cờ.
- **Reset (force cold)**: `<<<PHASE>>>` mới (cả advisor + `sup_runner.reset()`), `ch.resume()` sau restart (session
  không persist qua process → luôn cold), và **bất kỳ fallback ADR-15 nào** sang provider ≠ primary
  → `advisor_session=None` (session đặc thù provider, không chia sẻ).
- **Hòa giải ADR-15**: warm CHỈ áp profile primary (idx 0); `run_text_with_fallback` trả `provider`+`session_ref`,
  harness chỉ giữ session khi `provider == codex` (advisor) / `== kiro` (supervisor). Khi quota cạn → fallback
  opencode/kimi (cold) → khi primary hồi thì cold seed lại. **Warm bỏ `stop_re`** (cần JSONL `turn.completed`
  để lấy thread_id; idle/hard timeout vẫn chặn treo).
- **Config**: `Config.advisor_warm` (default True) / `supervisor_warm` (default **False** — xem trên),
  `advisor_warm_start_cmd/resume_cmd`, `supervisor_warm_resume_cmd`, `Limits.max_warm_turns_per_phase` (default 20).
  `load_config` nạp default khi field vắng → áp cho cả goal cũ. **Force-cold enforced** (chỉnh 2026-06-09): harness
  đếm warm turn trong phase; khi `>= max_warm_turns_per_phase` → `advisor_session=None` + re-seed FULL `a_ctx`
  (KHÔNG delta — session mới phải mang lại goal/contract/policy). **Bất biến giữ nguyên**: FSM/governance/verify/advisor_review không đọc session state; resume
  fail (JSONL `completed=False`) → `session=None` → cold lượt sau (không kẹt vòng resume hỏng).
- **Còn lại (ADR-16, đợt sau)**: memory ledger / agentmemory làm cầu nối cross-session/provider khi warm chết
  (cold sau fallback / goal mới) — chưa implement. `warm_until_task_done` cho role B (executor) cũng chưa (role B
  vẫn cold mỗi dispatch; ADR-14 đợt này chỉ làm advisor + supervisor per-phase).

### ADR-15 — Executor failure classification + provider fallback (chốt 2026-06-09)

**Vấn đề (live smoke E2E Smoke)**: khi role-CLI (Kiro) gặp rate-limit, nó retry nội bộ (`Retry #2/#3 within 10s`)
rồi hoặc exit hoặc bị idle-timeout kill — **không bao giờ ghi `<<<REPORT>>>` trailer**. Harness cũ chỉ đọc
`res.stdout` → `parse_report` trả `done=false` → Advisor **REJECT** → re-dispatch cùng role → lại rate-limit →
**vòng lặp đốt quota** tới khi chạm `max_turns`. Ba tình huống rất khác bản chất (B làm xong nhưng chưa đạt /
B không chạy được / B chạy nhưng không ghi report) bị gộp thành một `done=false`.

- **Quyết định**: tách một seam mới `parley/executor.py` (KHÔNG sở hữu governance) bọc `backend.run_once`, thêm:
  phân loại lỗi, stale-report protection, profile chain theo role, và per-error fallback policy. Harness chỉ
  nhận `ExecOutcome` (`report` hợp lệ **hoặc** `failed`) — không thấy chi tiết transport.
- **Ba outcome** executor phải phân biệt: (1) report hợp lệ (có trailer, **hoặc** report-file mới tạo/đổi hash)
  → ra Advisor review (kể cả `done=false`); (2) không chạy được (quota/account/cli) → fallback provider trong
  **cùng task**; (3) chạy nhưng không report (`missing_report`/`timeout`/`crash`) → **dừng sạch**, page human.
- **Stale-report protection**: hash report-file **trước** execution; chỉ chấp nhận backstop (file không trailer)
  khi file **mới tạo hoặc hash khác** — chặn nhận nhầm report cũ của attempt trước.
- **Phân loại lỗi** (signature **trước** timeout, vì kiro rate-limit retry tới khi idle-timeout kill → `timed_out`
  nhưng stdout nói "rate limit"): `rate_limited`, `usage_exhausted`, `auth_error`, `permission_denied`,
  `account_suspended`, `cli_unavailable`, `timeout`, `cli_exit_error`, `missing_report`. Chữ ký anchored context
  (không match `429` trần; tách 401 auth / 403 permission / suspension rõ ràng).
- **Per-error action**: `rate_limited` → retry ≤ `max_attempts` rồi next profile; `usage_exhausted`/`auth_error`/
  `account_suspended` → bỏ mọi profile cùng provider, sang provider khác; `cli_unavailable` → next profile;
  `permission_denied`/`timeout`/`cli_exit_error`/`missing_report` → **STOP** (fallback vô ích / không phải quota).
- **Routing đã kiểm chứng** (chỉ áp project/goal **mới**; project cũ không có `fallbacks` → chain = `[primary]`
  → fail là exhausted ngay, vẫn không đốt quota):
  - analyzer/architect/researcher (read-only): **kiro** → claude opus-4.8 (×3) → claude opus-4.7 (×3) → cursor `claude-opus-4-8-thinking-high`
  - reviewer (read-only): **opencode plan/qwen3.7-max** → glm-5.1 → qwen3.7-plus → cursor thinking
  - coder (edit): **opencode build/qwen3.7-plus** → minimax-m3 → glm-5.1 → cursor auto
  - fixer (edit): **opencode build/qwen3.7-max** → qwen3.7-plus → minimax-m3 → glm-5.1 → cursor auto
- **Transport**: kiro/claude/opencode nhận prompt qua **stdin**; **cursor** qua argv ngắn ("Read and execute
  PROMPT_PATH: <abs>") — không gửi prompt dài qua argv. `cursor_agent_path` cấu hình absolute (PATH uvicorn có
  thể chưa thấy cursor-agent). Claude **preflight** named-agent (`~/.claude/agents/<name>.*`); thiếu →
  `cli_unavailable` → next (không chạy claude default ngoài ý muốn). OpenCode spawn **tuần tự** (lock toàn cục:
  SQLite WAL không an toàn khi song song).
- **Events**: `executor_retry` (verbose), `executor_fallback` (milestone, kèm from/to provider/model),
  `executor_error`/`executor_exhausted` (escalate). Hết chain hoặc lỗi non-fallback → `stop("executor_stuck")`
  → goal `failed` → project `blocked` → Telegram escalate. **Advisor chỉ review khi có report hợp lệ.**
- **UI**: task card hiện provider/model + `fallback N`; timeline render swap; Task Detail bảng attempts
  (provider/model/reason mỗi lần). Lọc noise CLI (`WARNING: Agent conflict`, trust banner) khỏi excerpt hiển thị
  (`protocol.clean_excerpt`) — giữ raw stdout nguyên vẹn cho parse/classify.
- **Bất biến**: KHÔNG đổi logic Advisor/Supervisor (chỉ thêm tầng fallback transport); fallback giữ nguyên
  `task_id`/`slice`/`role`/`prompt_path`/`report_path`; FSM/governance không đọc provider/model.

**Mở rộng 2026-06-09 — Advisor/Supervisor quota fallback**: lý do "chạy liên tục" làm provider primary (codex
cho advisor, kiro cho supervisor) hết quota giữa chừng. Advisor/Supervisor KHÁC executor: không có report-file
để classify — "success" = caller parse được (advisor → directive; supervisor → JSON verdict).
- **`classify_text(stdout, exit, timed_out)`**: chỉ trả reason thuộc **QUOTA_FAMILY** (`rate_limited`,
  `usage_exhausted`, `auth_error`, `account_suspended`, `cli_unavailable`); còn lại trả `None` = "không phải
  lỗi transport, để caller tự xử". Nhờ vậy advisor `NONE/MULTIPLE` vẫn là content `turn_error`/`consec_err`
  như cũ; advisor timeout/crash vẫn theo đường cũ — fallback CHỈ chèn khi xuất hiện chữ ký quota.
- **`run_text_with_fallback`**: chạy `[primary]+fallbacks`, đổi profile chỉ trên quota-family (per-error action
  tái dùng). Trả `TextOutcome(stdout, res, provider, model, quota_failed, attempts)`; `stdout` luôn là lần chạy
  cuối để caller parse. Hết chain do quota → `quota_failed=True`.
- **Wiring**: advisor (harness loop) — `quota_failed` → `executor_exhausted(role=advisor)` → `stop("executor_stuck")`.
  Supervisor (`cli._sup_runner`) — fallback trong suốt, trả stdout cho `supervisor.gate` parse như cũ.
- **Chain đã kiểm chứng (1 tầng, khác họ executor để có lưới quota độc lập)**: advisor `codex → opencode/kimi-k2.6`
  (reasoning mạnh, long-context); supervisor `kiro → opencode/deepseek-v4-flash` (nhanh/rẻ, đủ cho verdict JSON).
  Cả hai dùng `opencode run --agent plan` (stdin) — tránh hoàn toàn rắc rối argv/prompt-động của cursor.
- **Signature fix**: thêm `hit your usage limit` / `usage limits will reset` / `spend limit` vào `usage_exhausted`
  (cursor-style; chữ ký cũ bỏ sót — phát hiện khi test `agent --model gpt-5.5-high`).
- **Khác executor routing**: advisor/supervisor fallback áp dụng **cả goal cũ** (qua `load_config` default khi
  field vắng), vì "chạy liên tục hết quota" ảnh hưởng mọi project — không chỉ goal mới.

**Trạng thái 2026-06-09**: executor.py + classify + chain + stale-protection + harness loop guard + events +
tasks projection + index.html UI implement xong; thêm advisor/supervisor quota fallback (`classify_text` +
`run_text_with_fallback`). `test_executor.py` 36 tests; full suite `143/143` xanh. Executor routing (coder/
fixer/reviewer/analyzer/architect) chỉ áp goal mới; advisor/supervisor fallback áp mọi goal. Live smoke executor
fallback đã xác nhận (stub rate-limit → fallback → report → done qua server/harness/notifier thật).

**Trạng thái 2026-06-09 (live smoke research goal — phát hiện + sửa 3 bug)**: chạy goal thật (analyzer trên repo
Parley) lộ ra:
- **Bug preflight claude (đã sửa)**: claude named-agent nằm trong **subdir** `~/.claude/agents/<category>/<name>.md`
  (vd `analysis/code-analyzer.md`, `core/researcher.md`, `sparc/architecture.md`), không ở top-level. Preflight cũ
  `glob("<name>.*")` chỉ quét top-level → bịa `cli_unavailable` dù claude chạy được (đã verify `claude --agent
  code-analyzer --model claude-opus-4.8` → exit 0). Sửa thành `rglob("<name>.md")` (đệ quy).
- **Timeout giờ là transient (đã sửa)**: `API_TIMEOUT_MS` là lỗi tạm thời (claude tự retry tới 10 lần). Đổi `timeout`
  từ STOP → `RETRYABLE` (retry trong profile) + `NEXT_PROFILE` (sang provider khác). Text-role: chain toàn timeout
  KHÔNG hard-fail (trả output cuối cho caller xử lý NONE→consec_err); chỉ hard-fail khi thấy lỗi quota-family thật.
- **Raw log mỗi attempt (đã thêm)**: `Attempt.raw_excerpt` (clean_excerpt ≤800) vào event `executor_*` + lưu raw đầy
  đủ `data_dir/attempts/<role>-<n>-<provider>-<model>-<reason>.log` — để truy vết khi phân loại sai. UI: timeline +
  bảng attempts (hover title) hiện raw_excerpt. `test_executor.py` 39 tests; full suite `146/146` xanh.

**Trạng thái 2026-06-07**: S1-S4 đã implement, unit suite `65/65` xanh. S4 thêm project-level
`run plan`: theo `contract.goal_ids`, chỉ spawn goal kế sau terminal event `stopped(reason=done)`;
`stopped|failed|paused|needs_human` chặn chuỗi; process exit thiếu terminal event → `failed/orphaned`
(fail-safe); `parallel` bị từ chối cho tới khi có worktree. CÒN để đóng ADR-13: live smoke GUI nhiều goal
với Codex/Kiro thật.

**Trạng thái 2026-06-07 cập nhật**: Plan→Goals hoàn thiện ở mức UI/API. `PUT /projects/{pid}/contract`
lưu draft đã sửa; `POST /projects/{pid}/approve?strategy=replace|append` tạo goals thật. Unit suite `67/67`
xanh; ShardX smoke xác nhận editor draft + nút `Approve & Replace`/`Approve & Append` hiển thị trên Pilonic.

**Trạng thái 2026-06-07 artifact**: Artifact Draft hoàn thiện ở mức UI/API. `POST /projects/{pid}/artifacts`
ghi file `.md/.txt/.json/.yaml/.yml` bên trong project sau khi user duyệt; path traversal/absolute path bị chặn.
Default path theo project name: `docs/<ProjectName>/domain-contract.md` (vd `docs/Pilonic/domain-contract.md`).
Unit suite `69/69` xanh; ShardX smoke xác nhận UI artifact trên Pilonic.

**Trạng thái 2026-06-07 prompt-file bridge**: Harness materialize prompt dài của role-agent vào
`data_dir/prompts/<task_id>.md` và dispatch event ghi `prompt_path` + `prompt_sha256`. Subprocess B chỉ nhận
prompt ngắn có `PROMPT_PATH`, `PROJECT_DIR`, `CONTRACT`, `SLICE`, `REPORT_PATH` và report protocol. Advisor vẫn
read-only; Advisor không tạo prompt-file/artifact trực tiếp. Unit suite `71/71` xanh.

**Issue thực tế 2026-06-08 — Goal 0 Pilonic**: `goal_f7d6a738` được tạo từ plan cũ chỉ có title
`Task 0 - Confirm Domain Contract`, không có `description`; config còn `contract_path=null` dù artifact
`docs/Pilonic/domain-contract.md` đã tồn tại. Runtime sinh `turn_error NONE` trước khi có `dispatch`, task-list trống,
sau đó user/observer stop. Fix: goal contract giữ `description` từ block task; domain-contract artifact tự set
`project.contract_path` và migrate config của goal không running; UI Goal detail hiển thị diagnostics
(`Missing goal details`, `contract_path`, `stop_reason`, `No task dispatched`) thay vì để cột task trống.

---

## 14. Tinh chỉnh sau review lần 2 (R2 — 2026-06-01)
| # | Mức | Vấn đề | Giải quyết |
|---|-----|--------|-----------|
| R2-1 | TRUNG BÌNH | VERIFY không nạp vào FSM → "done" không được thực thi | VERIFY mang `slice`; `fsm.observe_verify(slice,code)`; chuyển slice (strict) cần APPROVE **VÀ** verify_ok. Cập nhật loop 7.2, ADR-03/04, mục 8.5/8.6. |
| R2-2 | THẤP | Auto-commit/slice không có owner | Thêm module `gitio.py` + `commit_slice`; event `slice_done` lưu `commit` hash. |
| R2-3 | THẤP | Diagram mục 4 ngụ ý supervisor spawn | Chèn "Harness (CHU spawn)" giữa gate và spawn (khớp ADR-06). |
| R2-4 | THẤP | `verify.cmd` đơn-lệnh + ví dụ npm lệch toolchain | `verify.gates` (danh sách, PASS=tất cả exit 0); ví dụ pnpm+cargo+tsc+biome. |
| R2-5 | THẤP | `every_n_turns` khai báo nhưng loop không dùng | Loop: gate khi `turn % every_n_turns == 0`, lượt bỏ qua → default `continue`. |
| R2-6 | THẤP | Body có thể chứa END giả mang nonce | Closer = END cuối (outermost) + đúng một dòng-mở + cấm sentinel trong body (ADR-07 refinement). |
| R2-7 | THẤP | A không có plan bền + bootstrap khi chưa có contract | Contract là artifact neo (Kiro architect cập nhật); `contract_path=null` → A dispatch analyzer/architect tạo. |
| R2-8 | THẤP | Pause làm A chạy lại lượt, bỏ directive vừa sinh | Đọc `control.json` ở ĐẦU vòng lặp (pause/stop trước khi gọi A). |
| R2-9 | TRUNG BÌNH | Commit/slice_done sai nhánh → slice verify-cuối không bao giờ commit | Helper `maybe_commit(slice)` gọi sau MỌI đổi trạng thái FSM (cả nhánh VERIFY và report); `slice_done` True đúng 1 lần. |
| R2-10 | THẤP-TB | `turn` chỉ tăng ở nhánh report → MAX_TURNS không chặn A spin | Thêm `iters`/`hard_iter_cap` + `consec_errors`/`max_turn_errors` → stop("stuck"). |
| R2-11 | THẤP | Gate có thể bị bỏ qua cho edit-role khi `every_n_turns>1` | Bất biến: `gate_due = (turn%every_n_turns==0) or roles[d.role].edit` — edit:true LUÔN gate (mục 9). |

---

## 15. P0 verification + local agent setup (2026-06-01)
Môi trường: Python 3.13.3, codex-cli 0.135.0, kiro-cli 2.5.0 (Windows). Đã xác minh bằng `--help` + smoke test:
- `codex exec`: đọc prompt qua STDIN, exit 0; có `--sandbox read-only|workspace-write|danger-full-access`,
  `--ask-for-approval never`, `-C <dir>`, `-m/--model`, `-o/--output-last-message <FILE>` (dùng để parse directive sạch, né footer), `--json`.
- `kiro-cli chat`: `--no-interactive` + STDIN → trả lời, exit 0 (đã test "PONG"). Có `--agent`, `--trust-all-tools`,
  `--trust-tools=<set>`, `--model`. Output có mã ANSI + footer "Credits/Time" → parser phải strip ANSI + lấy trailer cuối.

### 15.1 Audit agent global (rủi ro phát hiện)
`allowedTools` global auto-trust (chạy không hỏi) cho MỌI role gồm `shell` + `code` → "read-only" roles
KHÔNG read-only thật. `orchestrator` global còn có `subagent` (tự đẻ sub-agent) + `web_search/web_fetch`
→ nếu spawn sẽ BỎ QUA harness/gate/log của Parley (Finding D, R1/R2).

### 15.2 Local override `.kiro/agents/` (đã tạo, validate exit 0, Workspace thắng Global)
Tạo bộ agent project-local ghi đè global, sửa đúng ranh giới Parley:
- read-only (analyzer/architect/reviewer + orchestrator neutralized): `allowedTools` = fs_read/read/grep/glob/code/fs_write/write — **BỎ `shell`** (cắt bề mặt exec tùy ý lớn nhất của R1) nhưng có `fs_write` để tự ghi report (option b).
- edit (coder/fixer): thêm `shell` (chạy build/test); spawn kèm `--trust-all-tools`.
- `orchestrator`: override vô hiệu `subagent`/`web_*`/`shell` → vá Finding D (Parley không dispatch tới nó; A=Codex orchestrate).
- Mỗi prompt nhúng **Parley report-protocol**: ghi report ra `report_path` + kết stdout bằng đúng trailer `<<<REPORT path=... done=... [verdict=...]>>>` (verdict chỉ reviewer) → giải quyết câu hỏi mở §11.

### 15.3 Ràng buộc runtime (CẦN CHỐT)
Kiro discover local agent theo CWD của tiến trình. Report path trong design là tương đối (`docs/reports/...`)
→ harness phải spawn kiro với **cwd = `project_dir`**, và bộ `.kiro/agents` này phải hiện diện tại `<project_dir>/.kiro/agents`.
Bản trong repo Parley là NGUỒN CANONICAL (version-controlled); harness/P0 cần bước sync copy sang `<project_dir>` trước khi chạy thật.

---

## 16. Lifecycle: init -> handover -> run (chốt 2026-06-01)
Hai pha:
- **INIT** (có người, một lần): `parley init` = code tất định bootstrap. LLM-supervisor CHỈ phán đoán ngữ nghĩa (goal->contract, xác nhận stack), KHÔNG chạy shell (giữ ADR-06).
- **RUN** (hands-free): vòng lặp mục 7.2; A điều phối, B thực thi, supervisor gate.

### 16.1 Hợp đồng `parley init <project_folder> --goal "..."`
1. Đăng ký `project_dir` theo PATH (không copy repo vào Parley).
2. Detect stack (dò `package.json`/`Cargo.toml`/`pnpm-lock`...) -> sinh `verify.gates` + ghi nhận ngôn ngữ. → giải Q2: KHÔNG hard-code ngôn ngữ vào agent; init suy ra.
3. Sync `.kiro/agents` canonical (repo Parley) -> `<project_dir>/.kiro/agents`. → giải Q1: harness chạy kiro với cwd=project_dir; bộ agent phải có ở đó.
4. Tạo nhánh git làm việc trong project_dir (chuẩn bị ADR-02); thêm `.kiro/agents/` vào `.gitignore` project (giữ `docs/reports/` làm artifact).
5. Lưu goal; LLM-supervisor phác thảo contract ban đầu, hoặc `contract_path=null` -> A bootstrap bằng dispatch analyzer/architect (R2-7).
6. Verify codex/kiro auth + chọn model (P0, mục 15).
7. Ghi `parley.config.yaml` + khởi tạo `data_dir`. **Idempotent**: chạy lại chỉ sync phần thiếu, không ghi đè goal/contract đang chạy, không tạo trùng nhánh.
8. Handover -> harness vào vòng lặp RUN.

### 16.2 Ranh giới init
- Detect/config/git/sync = code tất định (CLI `parley init`). LLM-supervisor = phán đoán, không shell.
- project_dir tham chiếu theo path; chỉ thêm `.kiro/agents` (+ `docs/reports` artifact) vào repo target.

---

## 17. Nhật ký
- 2026-06-07 — P1 LIVE SMOKE PASSED: `codex exec` (A) + `kiro-cli --agent` (B) trên sandbox git thật. Toàn bộ vòng đời chạy đúng: A dispatch `coder` (goal-driven) → supervisor `gov: continue` có reason thật (governance sống, hết `bad-json`) → coder tạo `hello.txt`="hello parley" + ghi report `.md`/snapshot/sha256 → `verify` exit 0 → A `COMPLETE` → `stopped: done`. Xác nhận: nonce-protocol, stop-on-sentinel, spawn (resolve PATHEXT/.cmd), per-turn re-seed goal+progress, report-file fallback, audit ndjson. Fix dọc đường: cờ `codex exec` (`--skip-git-repo-check`, bỏ `--ask-for-approval`), placeholder NONCE_HERE chống echo, parser supervisor bền (strip ANSI + flat-JSON), agent `supervisor` JSON-only.


---

## 18. GUI quản lý đa project/goal (mở rộng scope sau live-smoke)
Yêu cầu end-user: nhiều project, nhiều goal/project, xem tiến độ + hội thoại agent, sửa goal/project, config agent/cli/project. Engine KHÔNG đổi — thêm lớp quản lý + UI ở trên.

### 18.1 Mô hình
- Home `%USERPROFILE%\.parley` (hoặc `$PARLEY_HOME`): `registry.json` (projects+goals) + `configs/<goal_id>.json` + `data/<goal_id>/...`.
- Project = `{id, name, project_dir}`. Goal = một run độc lập `{id, project_id, goal, state, config_path, data_dir, pid}`.
- Chạy goal = spawn `parley run --config configs/<goal_id>.json` (engine sẵn có). Mỗi goal data riêng → song song được.

### 18.2 Các nấc
- A (xong, test): `store.py` (registry CRUD + sinh config/data per-goal qua `cli.init`) + `manager.py` (start/stop/control/refresh tiến trình theo goal).
- B (xong, code — cần fastapi để chạy): `web/app.py` API — projects/goals CRUD, run/control(pause/steer/stop), feed(SSE)/status, GET/PUT config; bearer token mọi route.
- C (xong, code): `web/index.html` UI vanilla JS — 3 view: Projects → Goals → Goal detail (timeline hội thoại realtime + nút run/pause/steer/stop + editor config).

### 18.3 Ranh giới & cảnh báo
- Stop mặc định = graceful (ghi control.json verdict=stop, harness dừng ở ranh giới lượt + reap agent con). `force=True` mới terminate (rủi ro agent con orphan trên Windows — TODO process-group kill).
- **Đồng thời nhiều goal trên CÙNG `project_dir`**: KHÔNG an toàn (chung working tree git, dễ xung đột). v1: mỗi project nên 1 goal active, hoặc tách project_dir. (Tương lai: git worktree per goal.)
- Sửa config/đường dẫn qua UI = bề mặt R1 → web localhost + bearer token + tunnel; validate project_dir (không production); roles.cmd vẫn whitelist do người duyệt.
- 2026-06-07 — ADR-11 implemented: advisor_review first-class (verdict bắt buộc sau mỗi report, event `advisor`, order report→advisor→gov→dispatch); commit gate HYBRID (advisor_review APPROVE [+verify nếu edit] → harness commit per slice; read-only slice commit khi APPROVE); capture `git diff` feed cho A; push-on-done (opt-in `git.auto_push`, chỉ `stop_reason=done`, work branch, never main/force); gitio guard protected-branch + no force. Config thêm `git{branch,auto_push,push_on,remote,protected}`. Unit test bổ sung. CÒN LẠI: ADR-12 housekeeping (agent-document + agent-git side-channel + write-lock).

---

## 19. Backlog sau ADR-13 — đối chiếu Wayland (2026-06-07)

Nguồn tham khảo kiến trúc: FerroxLabs/wayland, commit `1f304d2a88151b4e997e86fe912ce61b1102e855`
(`Wayland v0.9.6-rc.1`, 2026-06-06). Chỉ học mô hình/ý tưởng; KHÔNG sao chép code vì Wayland dùng
giấy phép `AGPL-3.0-or-later`.

### 19.1 Ranh giới sản phẩm
- Parley tiếp tục là **deterministic coding-governance harness**, không trở thành nền tảng agent tổng quát.
- Giữ bất biến: `Advisor đề xuất → Supervisor authorize → Harness spawn → Agent report → Advisor approve|reject → verify/commit`.
- Không đưa team tự tổ chức, mailbox tự do, channel/memory/extensions hoặc Electron rewrite vào scope hiện tại.
- Không bắt đầu backlog P1/P2 bên dưới trước khi **S4 sequential runner hoàn tất và ADR-13 đóng**.

### 19.2 P0 — đóng ADR-13
- [x] **S4 sequential project runner**: sau khi user approve contract, chạy từng goal theo đúng thứ tự; chỉ bắt đầu goal kế khi goal hiện tại `done`.
- [x] Dừng chuỗi khi goal `stopped|failed|paused|needs_human`; không tự chạy tiếp.
- [x] Resume an toàn sau restart dựa trên registry + terminal event; không chạy trùng goal đã `done`.
- [x] UI phản ánh project-run state và goal active/queued/done; task projection giữ theo goal đang chọn.
- [ ] Live smoke qua GUI: init → approve plan nhiều goal → run tuần tự → toàn bộ audit chain hợp lệ.

### 19.3 P1 — độ bền execution, làm sau ADR-13
- [ ] **Session policy (ADR-14) — mọi agent, warm-until-task-done**:
  - `SessionBackend` + `HeadlessResumeBackend` cho Advisor (`warm_per_phase`) và Init Advisor (`warm_per_project`).
  - **Executor (TẤT CẢ role B)**: mỗi `task_id` lưu `session_ref`; lần đầu **cold**; REJECT/chỉnh sửa cùng task → **warm**
    (delta prompt); task `done` → `session_end` → cold. Không hard-code chỉ coder/reviewer/fixer.
  - Harness: `resolve_session(task_id, role, slice, advisor_verdict)` → `cold|warm|force_cold` tất định;
    `max_warm_turns_per_task`; emit `session_end`.
  - Fail resume → force cold một lần; vẫn ghi audit đủ `session_ref_in/out`.
- [ ] **Project knowledge cho Init Advisor**: instructions, rules, decisions và references được lưu bền và inject có kiểm soát.
- [ ] **End-user intervention queue**: pause/steer/edit-goal/replan khi Advisor hoặc goal đang bận; lệnh có audit và thứ tự rõ.
- [ ] **`needs_human` escalation**: chuyển goal/task sang trạng thái cần người xử lý sau số lần reject/verify/fixer thất bại cấu hình được.
- [ ] **Mission Control projection**: ledger tổng hợp goal/task với running, queued, blocked, verifying, failed, needs-human,
  heartbeat/retry; event log vẫn là nguồn audit.

### 19.4 P2 — parallel execution có cô lập
- [ ] Chỉ hỗ trợ `execution_mode=parallel` khi mỗi goal có **git worktree riêng**; không cho nhiều writer chung working tree.
- [ ] Bổ sung dependency graph giữa goals/tasks (`blocked_by`) để scheduler chỉ chạy node sẵn sàng.
- [ ] Bổ sung lease/heartbeat/watchdog/retry budget để phát hiện tiến trình chết và recovery idempotent.
- [ ] Tách task state projection/ledger tối ưu khỏi append-only audit log; không tạo hai nguồn sự thật xung đột.
- [ ] Thiết kế merge/reconcile về branch đích và xử lý conflict qua Advisor + end-user approval.

### 19.5 Không áp dụng từ Wayland
- Không mở rộng thành desktop command center đa kênh hoặc hệ sinh thái extension trước khi governance runner ổn định.
- Không cho team lead/agent tự spawn ngoài Harness; mọi spawn vẫn qua Supervisor authorization và được audit.
- Không dùng verification fail-soft cho code-critical flow mặc định; policy nới lỏng phải explicit.
- Không đầu tư UI streaming/phối hợp agent phức tạp trước khi task/goal state machine và recovery được chốt.

### 19.6 Session policy — tóm tắt vận hành (ADR-14)

```
dispatch(task_id=T, role=R)
  |
  +-- task T chua co session HOAC force_cold --> COLD run_once --> luu session_ref
  |
  +-- task T dang mo (REJECT / revise / steer) --> WARM run_turn(session_ref, delta)
  |
  v
report --> advisor_review
  |
  +-- APPROVE (+ gate slice) --> session_end(task_done) --> task done --> lan dispatch sau: COLD
  |
  +-- REJECT, cung role R --> WARM cung task_id T (architect/analyzer chinh plan, fixer sua tiep, ...)
  |
  +-- REJECT, role khac (vd fixer) --> task_id moi T2 (emergent) --> COLD lan dau tren T2
```

**Token**: cold materialize prompt đầy đủ vào prompt-file để an toàn replay; B nhận STDIN ngắn có `PROMPT_PATH`.
Warm chỉ gửi delta + pointer report/contract/prompt path.
Advisor warm tránh lặp goal+contract+policy mỗi lượt trong cùng Phase.
