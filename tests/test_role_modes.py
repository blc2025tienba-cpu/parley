"""ADR-15 / LS-017 / LS-020: CLI permission-mode invariants for role profiles.

Each provider exposes a *capability mode* through its CLI flags. A read-only role
(analyzer/architect/researcher/reviewer) MUST run every profile in a read-only
mode; an edit role (coder/fixer) MUST run every profile in a write-capable mode.

Live smoke (goal_2859b601) showed why this matters: kiro under `--no-interactive`
*blocks on tool approval* unless a trust flag is present, so a read-only role with
NO trust flag silently fails ("Tool approval required", exit 1) before doing any
work. Conversely a read-only role must NOT be handed write capability.

This test classifies each profile's argv into one of:
  - "read"    : may read/search files, may NOT write/exec  (correct for read-only roles)
  - "write"   : may write files / run shell                (correct for edit roles)
  - "blocked" : no tools trusted -> kiro --no-interactive aborts on first tool use
  - "open"    : no explicit mode -> provider default (may prompt / unbounded) -> reject

and asserts the mode matches the role's `edit` flag for the PRIMARY and EVERY
fallback. It spawns no real CLI — pure inspection of `cli.default_roles()`.

NOTE (LS-020, documented in BACKLOG): a read-only mode also forbids writing the
report file, which conflicts with the report-trailer protocol. That architectural
conflict is tracked separately; this test only enforces mode<->role consistency.
"""
import unittest

from parley import cli


# Tools that mean "can mutate the workspace / run shell" for kiro --trust-tools.
_KIRO_WRITE_TOOLS = {"fs_write", "execute_bash", "execute_cmd"}
_KIRO_READ_TOOLS = {"fs_read"}


def classify_mode(cmd: list) -> str:
    """Map a profile argv to its capability mode: read | write | blocked | open."""
    prov = cli  # only for namespacing clarity
    arg0 = cmd[0].lower()

    if "kiro-cli" in arg0 or arg0 == "kiro":
        # find --trust-tools=<csv> or --trust-all-tools
        if "--trust-all-tools" in cmd or "-a" in cmd:
            return "write"
        trust = None
        for a in cmd:
            if a.startswith("--trust-tools="):
                trust = a.split("=", 1)[1]
                break
            if a == "--trust-tools":  # space-separated form (defensive)
                trust = ""  # treat as empty unless a following token is parsed
        if trust is None:
            return "open"          # no trust flag -> kiro --no-interactive will prompt -> abort
        tools = {t for t in trust.split(",") if t}
        if not tools:
            return "blocked"       # --trust-tools= (empty): no tools at all -> can't even read
        if tools & _KIRO_WRITE_TOOLS:
            return "write"
        if tools <= _KIRO_READ_TOOLS:
            return "read"
        return "open"              # unknown tool set -> be conservative

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
        # opencode agent profile: `plan` = read-only, `build` = write
        if "--agent" in cmd:
            i = cmd.index("--agent")
            if i + 1 < len(cmd):
                return "read" if cmd[i + 1] == "plan" else "write"
        return "open"

    return "open"


def _profiles(role_cfg: dict) -> list:
    """Primary cmd + every fallback cmd of a role."""
    out = [role_cfg["cmd"]]
    out += [fb["cmd"] for fb in role_cfg.get("fallbacks", [])]
    return out


class TestRoleModeInvariants(unittest.TestCase):
    def setUp(self):
        self.roles = cli.default_roles()

    def test_read_only_roles_never_open_or_blocked(self):
        """Every read-only profile must be classified 'read' — never write (leak),
        never blocked/open (LS-017: kiro w/o trust aborts before doing work)."""
        for name, rc in self.roles.items():
            if rc["edit"]:
                continue
            for cmd in _profiles(rc):
                mode = classify_mode(cmd)
                self.assertEqual(
                    mode, "read",
                    f"read-only role '{name}' profile {cmd} classified '{mode}' "
                    f"(expected 'read'; 'open'/'blocked' = LS-017 tool-approval abort, "
                    f"'write' = permission leak)")

    def test_edit_roles_are_write_capable(self):
        """Every edit-role profile must be write-capable, else it can't produce edits."""
        for name, rc in self.roles.items():
            if not rc["edit"]:
                continue
            for cmd in _profiles(rc):
                mode = classify_mode(cmd)
                self.assertEqual(
                    mode, "write",
                    f"edit role '{name}' profile {cmd} classified '{mode}' "
                    f"(expected 'write'; a read-only mode can't write files)")

    def test_kiro_read_only_has_fs_read_trust(self):
        """LS-017 regression guard: every kiro read-only profile MUST carry
        --trust-tools=fs_read (not empty, not missing, not write tools)."""
        for name, rc in self.roles.items():
            if rc["edit"]:
                continue
            for cmd in _profiles(rc):
                if "kiro-cli" not in cmd[0]:
                    continue
                trust = next((a.split("=", 1)[1] for a in cmd
                              if a.startswith("--trust-tools=")), None)
                self.assertIsNotNone(
                    trust, f"kiro read-only role '{name}' missing --trust-tools= ({cmd})")
                tools = {t for t in trust.split(",") if t}
                self.assertTrue(tools, f"kiro read-only role '{name}' has empty trust -> blocked")
                self.assertFalse(
                    tools & _KIRO_WRITE_TOOLS,
                    f"kiro read-only role '{name}' trusts write tools {tools & _KIRO_WRITE_TOOLS}")

    def test_classifier_self_check(self):
        """Pin the classifier so the invariant tests above can't silently pass on a
        broken classifier."""
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "analyzer",
             "--trust-tools=fs_read"]), "read")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "analyzer"]), "open")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--no-interactive", "--agent", "supervisor",
             "--trust-tools="]), "blocked")
        self.assertEqual(classify_mode(
            ["kiro-cli", "chat", "--agent", "coder", "--trust-tools=fs_read,fs_write"]), "write")
        self.assertEqual(classify_mode(
            ["claude", "-p", "--agent", "x", "--permission-mode", "plan"]), "read")
        self.assertEqual(classify_mode(
            ["claude", "-p", "--agent", "x", "--dangerously-skip-permissions"]), "write")
        self.assertEqual(classify_mode(
            ["cursor-agent", "-p", "--trust", "--mode", "plan"]), "read")
        self.assertEqual(classify_mode(
            ["cursor-agent", "-p", "--trust", "--force"]), "write")
        self.assertEqual(classify_mode(["opencode", "run", "--agent", "plan"]), "read")
        self.assertEqual(classify_mode(["opencode", "run", "--agent", "build"]), "write")


if __name__ == "__main__":
    unittest.main()
