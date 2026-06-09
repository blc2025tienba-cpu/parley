"""ADR-15 / LS-017 / LS-020 (hướng B): CLI capability-mode invariants for roles.

Hướng B (chốt 2026-06-10): MỌI role chạy CLI **write-capable** vì read-only roles
(analyzer/architect/researcher/reviewer) vẫn phải GHI report file vào REPORT_PATH —
một read-only CLI mode (kiro fs_read / claude plan / cursor plan / opencode plan) cấm
ghi nên không tạo được report (LS-020). Ràng buộc "không sửa source" chuyển sang MỀM:
SCOPE trong role prompt (context._scope_block) + advisor review diff.

Test này:
  1. classify_mode(): map argv -> read | write | blocked | open.
  2. MỌI profile (read-only và edit role) phải write-capable — KHÔNG 'open'/'blocked'
     (open/blocked = kiro --no-interactive abort, hoặc không ghi được report).
  3. SCOPE invariant: prompt read-only role phải mang ràng buộc "chỉ ghi REPORT_PATH";
     prompt edit role cho phép sửa source.
Không spawn CLI thật — thuần inspect cli.default_roles() + context.role_prompt_document.
"""
import unittest

from parley import cli, context
from parley.protocol import Directive


_KIRO_WRITE_TOOLS = {"fs_write", "execute_bash", "execute_cmd"}


def classify_mode(cmd: list) -> str:
    """Map a profile argv to its capability mode: read | write | blocked | open."""
    arg0 = cmd[0].lower()

    if "kiro-cli" in arg0 or arg0 == "kiro":
        if "--trust-all-tools" in cmd or "-a" in cmd:
            return "write"
        trust = None
        for a in cmd:
            if a.startswith("--trust-tools="):
                trust = a.split("=", 1)[1]
                break
        if trust is None:
            return "open"          # no trust flag -> kiro --no-interactive prompts -> abort
        tools = {t for t in trust.split(",") if t}
        if not tools:
            return "blocked"       # --trust-tools= (empty): no tools at all
        if tools & _KIRO_WRITE_TOOLS:
            return "write"
        return "read"              # only read tools (e.g. fs_read)

    if arg0 == "claude":
        if "--dangerously-skip-permissions" in cmd:
            return "write"
        if "--permission-mode" in cmd:
            i = cmd.index("--permission-mode")
            if i + 1 < len(cmd) and cmd[i + 1] == "plan":
                return "read"
            return "open"
        return "open"

    if "cursor-agent" in arg0 or arg0 == "cursor" or arg0 == "agent":
        if "--force" in cmd or "--yolo" in cmd:
            return "write"
        if "--plan" in cmd or ("--mode" in cmd and "plan" in cmd):
            return "read"
        return "open"

    if "opencode" in arg0:
        if "--agent" in cmd:
            i = cmd.index("--agent")
            if i + 1 < len(cmd):
                return "read" if cmd[i + 1] == "plan" else "write"
        return "open"

    return "open"


def _profiles(role_cfg: dict) -> list:
    out = [role_cfg["cmd"]]
    out += [fb["cmd"] for fb in role_cfg.get("fallbacks", [])]
    return out


class TestRoleModeInvariants(unittest.TestCase):
    def setUp(self):
        self.roles = cli.default_roles()

    def test_every_profile_is_write_capable(self):
        """Hướng B: MỌI profile (mọi role) phải write-capable để ghi được report file.
        'open'/'blocked' = kiro abort hoặc không ghi được report (LS-017/LS-020)."""
        for name, rc in self.roles.items():
            for cmd in _profiles(rc):
                mode = classify_mode(cmd)
                self.assertEqual(
                    mode, "write",
                    f"role '{name}' profile {cmd} classified '{mode}' (expected 'write': "
                    f"read-only roles vẫn phải ghi report -> cần write-capable CLI; "
                    f"'open'/'blocked' = tool-approval abort / không ghi được report)")

    def test_no_blocked_or_open_profiles(self):
        """Không profile nào được 'blocked' (empty trust) hay 'open' (no mode -> prompt)."""
        for name, rc in self.roles.items():
            for cmd in _profiles(rc):
                self.assertNotIn(
                    classify_mode(cmd), ("blocked", "open"),
                    f"role '{name}' profile {cmd} is blocked/open — sẽ treo hoặc abort")

    def test_read_only_role_prompt_forbids_source_edits(self):
        """SCOPE (ràng buộc mềm): prompt read-only role phải nêu rõ CHỈ ghi REPORT_PATH,
        cấm sửa file khác. Đây là lớp kiểm soát thay cưỡng chế CLI cứng (đã bỏ ở hướng B)."""
        d = Directive("DISPATCH", role="analyzer", slice="s1", prompt="khảo sát")
        doc = context.role_prompt_document(d, "C", "/proj", "c.md",
                                           "docs/reports/r.md", "task_1", edit=False)
        self.assertIn("SCOPE", doc)
        self.assertIn("REPORT_PATH", doc)
        # phải có ràng buộc cấm sửa file khác (read-only scope)
        low = doc.lower()
        self.assertTrue("không sửa" in low or "khong sua" in low or "cấm" in low or "cam" in low,
                        "read-only prompt thiếu ràng buộc cấm sửa source")

    def test_edit_role_prompt_allows_source_edits(self):
        """Prompt edit role cho phép tạo/sửa source theo slice."""
        d = Directive("DISPATCH", role="coder", slice="s1", prompt="implement")
        doc = context.role_prompt_document(d, "C", "/proj", "c.md",
                                           "docs/reports/r.md", "task_1", edit=True)
        self.assertIn("SCOPE", doc)
        self.assertIn("EDIT", doc.upper())

    def test_prompt_references_agents_md(self):
        """Hướng B: role prompt nhắc đọc AGENTS.md để đồng bộ contract chung."""
        d = Directive("DISPATCH", role="analyzer", slice="s1", prompt="x")
        doc = context.role_prompt_document(d, "C", "/proj", "c.md",
                                           "docs/reports/r.md", "task_1", edit=False)
        self.assertIn("AGENTS.md", doc)

    def test_classifier_self_check(self):
        """Pin classifier để các invariant trên không pass rỗng."""
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "analyzer",
             "--trust-all-tools"]), "write")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "analyzer",
             "--trust-tools=fs_read"]), "read")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "analyzer"]), "open")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--agent", "x", "--trust-tools="]), "blocked")
        self.assertEqual(classify_mode(
            ["claude", "-p", "--agent", "x", "--dangerously-skip-permissions"]), "write")
        self.assertEqual(classify_mode(
            ["claude", "-p", "--agent", "x", "--permission-mode", "plan"]), "read")
        self.assertEqual(classify_mode(
            ["cursor-agent", "-p", "--trust", "--force"]), "write")
        self.assertEqual(classify_mode(["opencode", "run", "--agent", "build"]), "write")
        self.assertEqual(classify_mode(["opencode", "run", "--agent", "plan"]), "read")


if __name__ == "__main__":
    unittest.main()
