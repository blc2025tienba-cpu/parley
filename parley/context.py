"""Parley context builders (ADR-01 pull-model). Pure string builders.

`{N}` is a nonce placeholder the harness substitutes with the per-turn nonce before spawn.
"""
from __future__ import annotations

_PROTO = (
    "# DIRECTIVE (BAT BUOC)\n"
    "Ban la ORCHESTRATOR. KHONG tu viet code, KHONG chay tool, KHONG giai thich dai dong.\n"
    'Moi luot CHI tra loi bang DUNG MOT directive o CUOI output, dung nonce: nonce="{N}"\n'
    "Cac dang hop le (thay NONCE_HERE = {N}):\n"
    '  <<<DISPATCH nonce="NONCE_HERE" role="..." slice="..." [review="APPROVE|REJECT"]>>>\n'
    "  <prompt nhieu dong gui cho B>\n"
    '  <<<END nonce="NONCE_HERE">>>\n'
    '  hoac  <<<VERIFY nonce="NONCE_HERE" slice="..." [review="APPROVE|REJECT"]>>>\n'
    '  hoac  <<<PHASE nonce="NONCE_HERE" id="..." reconciliation="..." [review="APPROVE|REJECT"]>>>\n'
    '  hoac  <<<COMPLETE nonce="NONCE_HERE" [review="APPROVE|REJECT"]>>>\n'
    "- NGAY SAU MOI REPORT, directive BAT BUOC co review=\"APPROVE|REJECT\" de ghi ro danh gia cua Advisor.\n"
    "role hop le: analyzer | architect | researcher | reviewer | coder | fixer."
)


_POLICY = (
    "# CACH LAM VIEC (orchestrator)\n"
    "- Muc tieu: hoan thanh GOAL o tren. Dieu phoi B (theo vai) de THUC SU lam xong, khong chi phan tich.\n"
    "- Luong dien hinh: (analyzer/architect neu can) -> coder tao/sua file -> reviewer -> VERIFY -> COMPLETE.\n"
    "- Goal don gian (vd tao 1 file) co the dispatch thang coder.\n"
    "- Sau moi report/verify: danh gia & quyet buoc KE TIEP tien toi GOAL.\n"
    "- MOI DISPATCH = MOT slice HEP, lam xong duoi ~10 phut. TUYET DOI khong gop nhieu viec lon vao\n"
    "  mot dispatch (vd: 'khao sat ca codebase + nghien cuu repo ngoai + viet plan' -> B se timeout,\n"
    "  mat sach output). Chia theo vai + pham vi: analyzer khao sat MOT vung code -> report; architect\n"
    "  doc report + thiet ke -> plan; coder tao file. Moi B chi nhan dung phan cua no.\n"
    "- Neu mot khao sat qua rong, chia thanh nhieu slice analyzer nho (theo module/file) roi tong hop.\n"
    "- CHI phat COMPLETE khi GOAL da THAT SU hoan thanh (file/thay doi can thiet da ton tai). "
    "TUYET DOI khong COMPLETE khi goal chua xong."
)


def _header(goal, contract, phase, prev_reconciliation):
    parts = [f"# GOAL\n{goal}", f"# PHASE\n{phase}"]
    parts.append(f"# CONTRACT\n{contract}" if contract else
                 "# CONTRACT\n(chua co) -> luot dau dispatch analyzer/architect de khao sat & lap ke hoach.")
    if prev_reconciliation:
        parts.append(f"# CARRYOVER (reconciliation phase truoc)\n{prev_reconciliation}")
    return parts


def _progress_block(progress):
    return ["# TIEN DO (cac buoc da lam trong phase nay)\n" + "\n".join(progress)] if progress else []


def advisor_seed(goal, contract, phase, prev_reconciliation=None) -> str:
    return "\n\n".join(_header(goal, contract, phase, prev_reconciliation) + [_POLICY, _PROTO])


def advisor_followup(goal, contract, phase, progress, report, prev_reconciliation=None) -> str:
    rep = (f"# REPORT MOI (slice={report.get('slice')}, role={report.get('role')}, "
           f"done={report.get('done')}, verdict={report.get('verdict')})\n"
           f"File: {report.get('report_path')}\nDoc full bang fs_read neu can. Trich:\n"
           f"{report.get('excerpt', '')}")
    blocks = [rep]
    if report.get("diff"):
        blocks.append("# DIFF THAY DOI THUC TE (review dua tren day, khong chi loi khai)\n"
                      + report["diff"])
    review = ("# ADVISOR REVIEW BAT BUOC\n"
              "Danh gia REPORT MOI (va DIFF neu co) la APPROVE hoac REJECT. Directive tiep theo "
              'BAT BUOC mang thuoc tinh review="APPROVE|REJECT".')
    blocks.append(review)
    return "\n\n".join(_header(goal, contract, phase, prev_reconciliation)
                       + _progress_block(progress) + blocks + [_POLICY, _PROTO])


def advisor_verify(goal, contract, phase, progress, verify, prev_reconciliation=None) -> str:
    v = (f"# KET QUA VERIFY\nexit={verify.get('exit')} failed_gate={verify.get('failed_gate')}\n"
         f"{verify.get('tail', '')}")
    return "\n\n".join(_header(goal, contract, phase, prev_reconciliation)
                       + _progress_block(progress) + [v, _POLICY, _PROTO])


def advisor_reject(goal, contract, phase, progress, why, prev_reconciliation=None) -> str:
    e = f"# LOI LUOT TRUOC\n{why}\nPhat lai DUNG MOT directive hop le (dang o duoi)."
    return "\n\n".join(_header(goal, contract, phase, prev_reconciliation)
                       + _progress_block(progress) + [e, _POLICY, _PROTO])


# ---- ADR-14 warm-session delta builders -------------------------------------
# On a WARM turn the advisor session already remembers GOAL/CONTRACT/POLICY and the
# orchestrator role, so we omit _header/_POLICY and send only the new info. We DO
# re-state the nonce contract (_NONCE_REMINDER) because the nonce changes every turn.
_NONCE_REMINDER = (
    "# DIRECTIVE (BAT BUOC)\n"
    'CHI tra loi bang DUNG MOT directive o CUOI output, dung nonce moi: nonce="{N}"\n'
    "Dang hop le: <<<DISPATCH role=.. slice=..>>>..<<<END>>> | <<<VERIFY slice=..>>> | "
    "<<<PHASE id=.. reconciliation=..>>> | <<<COMPLETE>>> "
    '(tat ca mang nonce="{N}"; sau report bat buoc review="APPROVE|REJECT").'
)


def advisor_followup_delta(progress, report) -> str:
    """Warm turn after a report. No header/policy — session remembers them."""
    rep = (f"# REPORT MOI (slice={report.get('slice')}, role={report.get('role')}, "
           f"done={report.get('done')}, verdict={report.get('verdict')})\n"
           f"File: {report.get('report_path')}\nTrich:\n{report.get('excerpt', '')}")
    blocks = [rep]
    if report.get("diff"):
        blocks.append("# DIFF THAY DOI THUC TE\n" + report["diff"])
    blocks.append("# ADVISOR REVIEW BAT BUOC\nDanh gia REPORT MOI (va DIFF neu co) "
                  'APPROVE hoac REJECT; directive tiep theo mang review="APPROVE|REJECT".')
    return "\n\n".join(_progress_block(progress) + blocks + [_NONCE_REMINDER])


def advisor_verify_delta(progress, verify) -> str:
    """Warm turn after a VERIFY run."""
    v = (f"# KET QUA VERIFY\nexit={verify.get('exit')} failed_gate={verify.get('failed_gate')}\n"
         f"{verify.get('tail', '')}")
    return "\n\n".join(_progress_block(progress) + [v, _NONCE_REMINDER])


def advisor_reject_delta(progress, why) -> str:
    """Warm turn after a malformed/invalid directive."""
    e = f"# LOI LUOT TRUOC\n{why}\nPhat lai DUNG MOT directive hop le."
    return "\n\n".join(_progress_block(progress) + [e, _NONCE_REMINDER])


def role_prompt_document(directive, contract, project_dir=None, contract_path=None,
                         report_path=None, task_id=None) -> str:
    rp = report_path or f"docs/reports/slice-{directive.slice}-{(directive.role or 'role')}-report.md"
    return (f"# PARLEY ROLE TASK\n"
            f"TASK_ID: {task_id}\nROLE: {directive.role}\nSLICE: {directive.slice}\n"
            f"PROJECT_DIR: {project_dir}\nCONTRACT: {contract_path}\nREPORT_PATH: {rp}\n\n"
            f"# ADVISOR DIRECTIVE\n{directive.prompt}\n\n"
            f"# CONTRACT CONTENT\n{contract or '(none)'}\n\n"
            f"{_EXEC_PROTO.format(RP=rp)}")


def executor_input(directive, contract, project_dir=None, contract_path=None, report_path=None,
                   prompt_path=None) -> str:
    rp = report_path or f"docs/reports/slice-{directive.slice}-{(directive.role or 'role')}-report.md"
    if prompt_path:
        return ("Ban la role-agent cua Parley. Doc FULL task prompt tai PROMPT_PATH truoc khi lam.\n"
                "Khong suy dien task tu prompt ngan nay; thuc thi dung tai lieu tai PROMPT_PATH.\n\n"
                f"PROMPT_PATH: {prompt_path}\nPROJECT_DIR: {project_dir}\n"
                f"CONTRACT: {contract_path}\nSLICE: {directive.slice}\nREPORT_PATH: {rp}\n\n"
                f"{_EXEC_PROTO.format(RP=rp)}")
    return (f"{directive.prompt}\n\n---\nPROJECT_DIR: {project_dir}\n"
            f"CONTRACT: {contract_path}\nSLICE: {directive.slice}\nREPORT_PATH: {rp}\n\n"
            f"{_EXEC_PROTO.format(RP=rp)}")


_EXEC_PROTO = (
    "## Parley report (BẮT BUỘC, provider-agnostic)\n"
    "Hoàn thành task của role, GHI report Markdown vào đúng REPORT_PATH ở trên, rồi KẾT output bằng "
    "ĐÚNG MỘT dòng cuối, không có gì sau nó:\n"
    '  <<<REPORT path="{RP}" done="true|false"[ verdict="APPROVE|REJECT"]>>>\n'
    "- `verdict` CHỈ khi role là reviewer (APPROVE/REJECT). Role khác bỏ qua.\n"
    "- KHÔNG in lại chuỗi <<<REPORT ...>>> ở bất kỳ chỗ nào khác."
)
