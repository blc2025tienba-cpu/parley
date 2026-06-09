import unittest
from pathlib import Path

from _util import temporary_directory
from parley.backends import RunResult
from parley.config import Role
from parley import executor


# ---- classify ----------------------------------------------------------------
class TestClassify(unittest.TestCase):
    def test_valid_report_wins(self):
        # has_trailer True -> None (a report, even if stdout also mentions a limit)
        self.assertIsNone(executor.classify("rate limit reached", 0, True, True, False))

    def test_file_changed_wins(self):
        self.assertIsNone(executor.classify("anything", 0, False, False, True))

    def test_rate_limit_before_timeout(self):
        # the live case: kiro retries then gets idle-killed -> timed_out True, but
        # stdout says rate limit. Signature must win so we fall back, not fail-fast.
        out = "WARNING: Retry #2, retrying within 10.0s..\n rate limit reached"
        self.assertEqual(executor.classify(out, -1, True, False, False), executor.RATE_LIMITED)

    def test_clean_timeout(self):
        self.assertEqual(executor.classify("still working...", -1, True, False, False),
                         executor.TIMEOUT)

    def test_bare_429_token_is_not_rate_limit(self):
        # "used 429 tokens" must NOT trigger rate_limited (context-anchored regex).
        self.assertEqual(executor.classify("used 429 tokens, exit 0", 0, False, False, False),
                         executor.MISSING_REPORT)

    def test_http_429_is_rate_limit(self):
        self.assertEqual(executor.classify("HTTP 429 Too Many Requests", 0, False, False, False),
                         executor.RATE_LIMITED)

    def test_auth_vs_permission_vs_suspended(self):
        self.assertEqual(executor.classify("HTTP 401 unauthorized", 0, False, False, False),
                         executor.AUTH_ERROR)
        self.assertEqual(executor.classify("HTTP 403 forbidden", 0, False, False, False),
                         executor.PERMISSION_DENIED)
        self.assertEqual(executor.classify("your account has been suspended", 0, False, False, False),
                         executor.ACCOUNT_SUSPENDED)

    def test_usage_exhausted(self):
        self.assertEqual(executor.classify("credit balance is too low", 0, False, False, False),
                         executor.USAGE_EXHAUSTED)

    def test_cli_unavailable(self):
        self.assertEqual(executor.classify(
            "'claude' is not recognized as an internal or external command", 1, False, False, False),
            executor.CLI_UNAVAILABLE)

    def test_clean_exit_no_report(self):
        self.assertEqual(executor.classify("done thinking", 0, False, False, False),
                         executor.MISSING_REPORT)

    def test_nonzero_exit_no_signature(self):
        self.assertEqual(executor.classify("segfault", 134, False, False, False),
                         executor.CLI_EXIT_ERROR)


class TestFallbackAction(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(executor.fallback_action(executor.RATE_LIMITED), executor.NEXT_PROFILE)
        self.assertEqual(executor.fallback_action(executor.USAGE_EXHAUSTED), executor.SKIP_PROVIDER)
        self.assertEqual(executor.fallback_action(executor.AUTH_ERROR), executor.SKIP_PROVIDER)
        self.assertEqual(executor.fallback_action(executor.ACCOUNT_SUSPENDED), executor.SKIP_PROVIDER)
        # LS-022: permission_denied (e.g. read-only mode blocked the report write) ->
        # try the next profile/provider, which may run in a write-capable mode.
        self.assertEqual(executor.fallback_action(executor.PERMISSION_DENIED), executor.NEXT_PROFILE)
        self.assertEqual(executor.fallback_action(executor.CLI_UNAVAILABLE), executor.NEXT_PROFILE)
        # ADR-15 update: timeout is transient (API_TIMEOUT_MS) -> retry then next profile
        self.assertEqual(executor.fallback_action(executor.TIMEOUT), executor.NEXT_PROFILE)
        self.assertEqual(executor.fallback_action(executor.MISSING_REPORT), executor.STOP)


# ---- stale-report protection -------------------------------------------------
class TestStaleProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "r.md"

    def test_missing_file(self):
        self.assertEqual(executor.file_sig(str(self.p)), (False, None))
        self.assertFalse(executor.file_changed((False, None), str(self.p)))

    def test_new_file_is_change(self):
        before = executor.file_sig(str(self.p))     # absent
        self.p.write_text("hi", encoding="utf-8")
        self.assertTrue(executor.file_changed(before, str(self.p)))

    def test_unchanged_file_is_not_change(self):
        self.p.write_text("hi", encoding="utf-8")
        before = executor.file_sig(str(self.p))
        self.assertFalse(executor.file_changed(before, str(self.p)))

    def test_modified_file_is_change(self):
        self.p.write_text("hi", encoding="utf-8")
        before = executor.file_sig(str(self.p))
        self.p.write_text("bye", encoding="utf-8")
        self.assertTrue(executor.file_changed(before, str(self.p)))


# ---- fallback chain ----------------------------------------------------------
class ScriptBackend:
    """Returns a queued RunResult per call; records the commands it was given.

    LS-021: a real role that emits a <<<REPORT>>> trailer also WRITES the report
    file. To keep tests realistic (a trailer alone is no longer accepted as success),
    when `report_abs` is set this backend writes a non-empty report file whenever the
    returned stdout contains a trailer. Set report_abs=None to simulate a PHANTOM
    trailer (claimed report, no file) for the LS-021 regression test."""
    def __init__(self, results, report_abs="__auto__"):
        self.results = list(results)
        self.report_abs = report_abs   # "__auto__" -> _run fills it; None -> phantom (no file)
        self.calls = []   # (provider-ish argv0, full cmd, stdin)

    def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
        self.calls.append((profile.cmd[0], list(profile.cmd), prompt))
        res = self.results.pop(0) if self.results else RunResult("", 0)
        if self.report_abs and "<<<REPORT" in (res.stdout or ""):
            from pathlib import Path as _P
            p = _P(self.report_abs)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# report\nwork done\n", encoding="utf-8")
        return res


def _role(primary_cmd, fallbacks, edit=False):
    return Role(cmd=primary_cmd, edit=edit, fallbacks=fallbacks)


class TestRunWithFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name)
        self.rp_rel = "r.md"
        self.rp_abs = str(self.proj / self.rp_rel)
        self.events = []

    def _emit(self, etype, **kw):
        self.events.append({"type": etype, **kw})

    def _run(self, role_cfg, backend):
        # LS-021: by default the backend materializes the report file on a trailer
        # (realistic). A test wanting a PHANTOM trailer passes ScriptBackend(..., report_abs=None).
        if getattr(backend, "report_abs", None) == "__auto__":
            backend.report_abs = self.rp_abs
        return executor.run_with_fallback(
            "coder", role_cfg, backend, stdin_input="do it",
            prompt_path=str(self.proj / "p.md"), report_rel=self.rp_rel,
            report_abs=self.rp_abs, cwd=str(self.proj), idle_timeout=1, hard_timeout=1,
            emit=self._emit)

    def test_primary_report_no_fallback(self):
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult(report, 0)])
        role = _role(["kiro-cli", "x"], [{"provider": "claude", "cmd": ["claude"]}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        self.assertEqual(len(be.calls), 1)                    # never touched fallback
        self.assertFalse(any(e["type"] == "executor_fallback" for e in self.events))

    def test_done_false_with_trailer_is_valid(self):
        be = ScriptBackend([RunResult('<<<REPORT path="r.md" done="false">>>', 0)])
        role = _role(["kiro-cli", "x"], [])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        self.assertFalse(out.report.done)

    def test_rate_limit_then_next_profile_succeeds(self):
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult("rate limit reached", 0), RunResult(report, 0)])
        role = _role(["kiro-cli", "x"],
                     [{"provider": "opencode", "model": "m", "cmd": ["opencode", "run"]}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        self.assertEqual(out.provider, "opencode")
        fb = [e for e in self.events if e["type"] == "executor_fallback"]
        self.assertEqual(len(fb), 1)
        self.assertEqual((fb[0]["from_provider"], fb[0]["to_provider"]), ("kiro", "opencode"))
        self.assertEqual(fb[0]["reason"], executor.RATE_LIMITED)

    def test_rate_limit_retries_within_profile(self):
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult("rate limit reached", 0),
                            RunResult("rate limit reached", 0),
                            RunResult(report, 0)])
        # one profile, max_attempts=3 -> retries same profile twice then succeeds
        role = _role(["claude", "x"],
                     [])
        role.fallbacks = []
        role.cmd = ["claude", "-p"]
        # primary max_attempts is always 1; use a fallback with max_attempts=3 and make
        # primary fail with cli_unavailable-free rate_limit so we land on the retrying one.
        role = _role(["kiro-cli", "x"],
                     [{"provider": "opencode", "model": "m", "cmd": ["opencode"], "max_attempts": 3}])
        be = ScriptBackend([RunResult("rate limit reached", 0),   # kiro primary -> next
                            RunResult("rate limit reached", 0),   # opencode attempt1 -> retry
                            RunResult("rate limit reached", 0),   # opencode attempt2 -> retry
                            RunResult(report, 0)])                # opencode attempt3 -> ok
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        retries = [e for e in self.events if e["type"] == "executor_retry"]
        self.assertEqual(len(retries), 2)

    def test_usage_exhausted_skips_same_provider(self):
        report = '<<<REPORT path="r.md" done="true">>>'
        # primary opencode/A usage_exhausted -> must SKIP opencode/B -> land on cursor
        be = ScriptBackend([RunResult("credit balance is too low", 0), RunResult(report, 0)])
        role = _role(["opencode", "run", "--model", "A"],
                     [{"provider": "opencode", "model": "B", "cmd": ["opencode", "run", "--model", "B"]},
                      {"provider": "cursor", "model": "auto", "cmd": ["cursor-agent"],
                       "input": "argv_prompt"}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        self.assertEqual(out.provider, "cursor")
        # opencode/B was skipped: only 2 spawns (opencode/A, cursor)
        self.assertEqual(len(be.calls), 2)

    def test_permission_denied_falls_back(self):
        # LS-022: a read-only provider blocked from writing the report (permission_denied)
        # must FALL BACK to the next profile/mode, not STOP — another provider may write.
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult("HTTP 403 forbidden", 0), RunResult(report, 0)])
        role = _role(["kiro-cli", "x"], [{"provider": "claude", "cmd": ["claude"]}])
        out = self._run(role, be)                             # backend auto-writes file on trailer
        self.assertEqual(out.kind, "report")                  # recovered on claude fallback
        self.assertEqual(out.provider, "claude")
        fb = [e for e in self.events if e["type"] == "executor_fallback"]
        self.assertEqual(fb[0]["reason"], executor.PERMISSION_DENIED)
        self.assertEqual(len(be.calls), 2)

    def test_timeout_falls_back_then_succeeds(self):
        # ADR-15: timeout is transient (API_TIMEOUT_MS) -> try next profile, not fail-fast.
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult("working", -1, True), RunResult(report, 0)])
        role = _role(["kiro-cli", "x"],
                     [{"provider": "opencode", "model": "m", "cmd": ["opencode"]}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")           # recovered on the next profile
        self.assertEqual(out.provider, "opencode")
        fb = [e for e in self.events if e["type"] == "executor_fallback"]
        self.assertEqual(fb[0]["reason"], executor.TIMEOUT)
        self.assertEqual(len(be.calls), 2)

    def test_timeout_retries_within_profile(self):
        # timeout is in RETRYABLE: a profile with max_attempts>1 retries before moving on
        report = '<<<REPORT path="r.md" done="true">>>'
        be = ScriptBackend([RunResult("working", -1, True),   # kiro primary -> next
                            RunResult("slow", -1, True),       # opencode attempt1 -> retry
                            RunResult(report, 0)])             # opencode attempt2 -> ok
        role = _role(["kiro-cli", "x"],
                     [{"provider": "opencode", "model": "m", "cmd": ["opencode"], "max_attempts": 2}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "report")
        retries = [e for e in self.events if e["type"] == "executor_retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0]["reason"], executor.TIMEOUT)

    def test_chain_exhausted(self):
        be = ScriptBackend([RunResult("rate limit reached", 0),
                            RunResult("rate limit reached", 0)])
        role = _role(["kiro-cli", "x"],
                     [{"provider": "opencode", "model": "m", "cmd": ["opencode"]}])
        out = self._run(role, be)
        self.assertEqual(out.kind, "failed")
        self.assertTrue(out.exhausted)
        self.assertEqual(len(out.attempts), 2)

    def test_file_backstop_only_when_changed(self):
        # no trailer, but the backend writes the report file during the run -> accept
        report_written = ScriptBackend([RunResult("did work no trailer", 0)])

        def run_once(profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
            report_written.calls.append((profile.cmd[0], list(profile.cmd), prompt))
            Path(self.rp_abs).write_text("# report", encoding="utf-8")
            return RunResult("did work no trailer", 0)
        report_written.run_once = run_once
        role = _role(["kiro-cli", "x"], [])
        out = self._run(role, report_written)
        self.assertEqual(out.kind, "report")
        self.assertTrue(out.report.done)

    def test_stale_file_not_accepted(self):
        # file exists BEFORE the run and backend doesn't touch it -> missing_report, not report
        Path(self.rp_abs).write_text("stale", encoding="utf-8")
        be = ScriptBackend([RunResult("no trailer, no write", 0)])
        role = _role(["kiro-cli", "x"], [])
        out = self._run(role, be)
        self.assertEqual(out.kind, "failed")
        self.assertEqual(out.reason, executor.MISSING_REPORT)

    def test_phantom_trailer_without_file_is_missing_report(self):
        # LS-021: a role prints the <<<REPORT>>> trailer but never writes the file
        # (read-only blocked, or it just forgot). The trailer alone must NOT be accepted
        # -> MISSING_REPORT (and STOP cleanly to page a human, not feed A a phantom report).
        report = '<<<REPORT path="r.md" done="true">>>'
        # report_abs=None -> backend does NOT materialize the file (phantom trailer)
        be = ScriptBackend([RunResult(report, 0)], report_abs=None)
        role = _role(["kiro-cli", "x"], [])
        out = self._run(role, be)
        self.assertEqual(out.kind, "failed")
        self.assertEqual(out.reason, executor.MISSING_REPORT)


class TestProfileShaping(unittest.TestCase):
    def test_cursor_argv_prompt_injected(self):
        # cursor profile: prompt goes via argv, not stdin
        captured = {}

        class B:
            def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
                captured["cmd"] = list(profile.cmd)
                captured["stdin"] = prompt
                return RunResult('<<<REPORT path="r.md" done="true">>>', 0)

        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name)
        role = _role(["cursor-agent", "-p"], [], edit=True)
        role.cmd = ["cursor-agent", "-p"]
        # make the PRIMARY a cursor argv_prompt profile by using fallback shape
        role = _role(["kiro-cli", "x"],
                     [{"provider": "cursor", "model": "auto", "cmd": ["cursor-agent", "-p"],
                       "input": "argv_prompt"}])
        be = B()
        # force kiro to fail so we reach cursor
        seq = ScriptBackend([RunResult("rate limit reached", 0)])

        class Chain:
            def __init__(self): self.n = 0
            def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
                self.n += 1
                if self.n == 1:
                    return RunResult("rate limit reached", 0)
                captured["cmd"] = list(profile.cmd)
                captured["stdin"] = prompt
                Path(proj / "r.md").write_text("# report body", encoding="utf-8")   # LS-021: real file
                return RunResult('<<<REPORT path="r.md" done="true">>>', 0)

        out = executor.run_with_fallback(
            "coder", role, Chain(), stdin_input="STDIN-PROMPT",
            prompt_path=str(proj / "p.md"), report_rel="r.md",
            report_abs=str(proj / "r.md"), cwd=str(proj), idle_timeout=1, hard_timeout=1,
            cursor_agent_path=None, emit=lambda *a, **k: None)
        self.assertEqual(out.kind, "report")
        # cursor: stdin empty, argv carries the short prompt referencing PROMPT_PATH
        self.assertEqual(captured["stdin"], "")
        self.assertTrue(any("Read and execute" in a for a in captured["cmd"]))

    def test_cursor_agent_path_override(self):
        captured = {}

        class Chain:
            def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
                captured["arg0"] = profile.cmd[0]
                Path(proj / "r.md").write_text("# report body", encoding="utf-8")   # LS-021: real file
                return RunResult('<<<REPORT path="r.md" done="true">>>', 0)

        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name)
        role = _role(["cursor-agent", "-p"],
                     [], edit=True)
        # primary is cursor with argv_prompt; provide an absolute override path
        role = Role(cmd=["cursor-agent", "-p"], edit=True, fallbacks=[])
        out = executor.run_with_fallback(
            "coder", role, Chain(), stdin_input="x",
            prompt_path=str(proj / "p.md"), report_rel="r.md",
            report_abs=str(proj / "r.md"), cwd=str(proj), idle_timeout=1, hard_timeout=1,
            cursor_agent_path=r"C:\abs\agent.cmd", emit=lambda *a, **k: None)
        # primary cursor profile is inferred as provider 'cursor' -> arg0 overridden
        self.assertEqual(captured["arg0"], r"C:\abs\agent.cmd")


class TestClaudePreflight(unittest.TestCase):
    """The preflight bug: real claude agents live in subdirs as <name>.md
    (e.g. ~/.claude/agents/analysis/code-analyzer.md), so a top-level glob missed
    them and wrongly returned cli_unavailable for a working claude install."""
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".claude" / "agents" / "analysis").mkdir(parents=True)
        (self.home / ".claude" / "agents" / "analysis" / "code-analyzer.md").write_text(
            "---\nname: code-analyzer\n---\n", encoding="utf-8")

    def test_subdir_agent_is_found(self):
        import unittest.mock as mock
        with mock.patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(executor._claude_agent_available("code-analyzer"))   # subdir .md
            self.assertFalse(executor._claude_agent_available("nonexistent-agent"))
            self.assertTrue(executor._claude_agent_available(None))               # no agent flag -> ok


# ---- classify_text (advisor/supervisor, no report file) ---------------------
class TestClassifyText(unittest.TestCase):
    def test_quota_signature_returns_reason(self):
        self.assertEqual(executor.classify_text("rate limit reached", 0, False),
                         executor.RATE_LIMITED)

    def test_cursor_usage_limit_now_detected(self):
        # the exact string the live cursor account returned — previously missed.
        out = ("You've hit your usage limit. Switch to a different model or set a "
               "spend limit. Your usage limits will reset when your cycle ends.")
        self.assertEqual(executor.classify_text(out, 1, False), executor.USAGE_EXHAUSTED)

    def test_content_output_is_not_a_failure(self):
        # a normal advisor directive / supervisor JSON has no quota signature -> None
        self.assertIsNone(executor.classify_text('<<<COMPLETE nonce="ab">>>', 0, False))
        self.assertIsNone(executor.classify_text('{"verdict":"continue"}', 0, False))

    def test_timeout_is_actionable_for_text_roles(self):
        # ADR-15 update: timeout is transient (API_TIMEOUT_MS) -> actionable so the
        # chain retries/falls back, but it is NOT quota (won't hard-fail the goal).
        self.assertEqual(executor.classify_text("still thinking", -1, True), executor.TIMEOUT)
        self.assertNotIn(executor.TIMEOUT, executor.QUOTA_FAMILY)


class TestRunTextWithFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name)
        self.events = []

    def _emit(self, etype, **kw):
        self.events.append({"type": etype, **kw})

    def _run(self, primary, fallbacks, backend):
        return executor.run_text_with_fallback(
            "advisor", primary, fallbacks, backend, stdin_input="ctx",
            cwd=str(self.proj), idle_timeout=1, hard_timeout=1, emit=self._emit)

    def test_primary_content_no_fallback(self):
        # primary returns a directive (non-quota) -> returned as-is, no fallback
        be = ScriptBackend([RunResult('<<<COMPLETE nonce="x">>>', 0)])
        out = self._run(["codex", "exec"],
                        [{"provider": "opencode", "model": "k", "cmd": ["opencode", "run"]}], be)
        self.assertFalse(out.quota_failed)
        self.assertEqual(out.provider, "codex")
        self.assertEqual(len(be.calls), 1)
        self.assertFalse(any(e["type"] == "executor_fallback" for e in self.events))

    def test_primary_none_directive_is_not_quota(self):
        # advisor returns garbage (would be NONE) but it's NOT a quota signature ->
        # returned as-is for the harness to handle as a content turn_error.
        be = ScriptBackend([RunResult("I think we should...", 0)])
        out = self._run(["codex", "exec"], [{"provider": "opencode", "cmd": ["opencode"]}], be)
        self.assertFalse(out.quota_failed)
        self.assertEqual(len(be.calls), 1)            # did NOT fall back on a content error

    def test_quota_then_fallback_provider(self):
        be = ScriptBackend([RunResult("rate limit reached", 0),
                            RunResult('<<<COMPLETE nonce="x">>>', 0)])
        out = self._run(["codex", "exec"],
                        [{"provider": "opencode", "model": "opencode-go/kimi-k2.6",
                          "cmd": ["opencode", "run", "--model", "opencode-go/kimi-k2.6"]}], be)
        self.assertFalse(out.quota_failed)
        self.assertEqual(out.provider, "opencode")
        fb = [e for e in self.events if e["type"] == "executor_fallback"]
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]["role"], "advisor")
        self.assertEqual((fb[0]["from_provider"], fb[0]["to_provider"]), ("codex", "opencode"))

    def test_all_quota_exhausted(self):
        be = ScriptBackend([RunResult("rate limit reached", 0),
                            RunResult("credit balance is too low", 0)])
        out = self._run(["codex", "exec"],
                        [{"provider": "opencode", "cmd": ["opencode", "run"]}], be)
        self.assertTrue(out.quota_failed)
        self.assertEqual(len(out.attempts), 2)

    def test_all_timeout_does_not_hard_fail(self):
        # ADR-15: a chain that only ever times out is transient, NOT quota. Hand the
        # last output back (quota_failed=False) so the caller's NONE->turn_error /
        # consec_err budget governs instead of hard-failing the goal.
        be = ScriptBackend([RunResult("still thinking", -1, True),
                            RunResult("still thinking", -1, True)])
        out = self._run(["codex", "exec"],
                        [{"provider": "opencode", "cmd": ["opencode", "run"]}], be)
        self.assertFalse(out.quota_failed)
        self.assertEqual(len(out.attempts), 2)
        # both attempts tried (timeout falls back across the chain)
        self.assertTrue(all(a["reason"] == executor.TIMEOUT for a in executor._attempt_dicts(out.attempts)))


# ---- resolve_session_id (codex JSONL thread_id) -----------------------------
_CODEX_JSONL = (
    '{"thread_id": "th_abc123"}\n'
    '{"type": "item.completed", "item": {"text": "<<<COMPLETE nonce=\\"x\\">>>"}}\n'
    '{"type": "turn.completed"}\n'
)


class TestResolveSessionId(unittest.TestCase):
    def test_codex_thread_id_when_completed(self):
        self.assertEqual(executor.resolve_session_id("codex", _CODEX_JSONL), "th_abc123")

    def test_codex_none_when_not_completed(self):
        # no turn.completed -> id is not trusted (resume would fail)
        partial = '{"thread_id": "th_abc123"}\n{"type": "item.completed", "item": {"text": "x"}}\n'
        self.assertIsNone(executor.resolve_session_id("codex", partial))

    def test_non_codex_returns_none(self):
        self.assertIsNone(executor.resolve_session_id("kiro", "Chat SessionId: 123"))


# ---- ADR-14 warm sessions (advisor/supervisor) ------------------------------
class CmdRecordBackend:
    """Records the exact argv of each run; returns queued RunResults in order."""
    def __init__(self, results):
        self.results = list(results)
        self.cmds = []

    def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
        self.cmds.append((list(profile.cmd), prompt))
        return self.results.pop(0) if self.results else RunResult("", 0)


class TestWarmTextFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name)
        self.start_cmd = ["codex", "exec", "--json", "--sandbox", "read-only", "-"]
        self.resume_cmd = ["codex", "exec", "resume", "--json", "{session_id}", "-"]

    def _warm(self, backend, session_id=None, warm_stdin=None):
        return executor.run_text_with_fallback(
            "advisor", ["codex", "exec"], [], backend, stdin_input="FULL SEED",
            cwd=str(self.proj), idle_timeout=1, hard_timeout=1, warm=True,
            session_id=session_id, warm_start_cmd=self.start_cmd,
            warm_resume_cmd=self.resume_cmd, warm_stdin=warm_stdin)

    def test_cold_first_turn_uses_start_cmd_and_returns_session(self):
        be = CmdRecordBackend([RunResult(_CODEX_JSONL, 0)])
        out = self._warm(be, session_id=None)
        self.assertEqual(out.session_ref, "th_abc123")    # thread_id captured
        self.assertEqual(be.cmds[0][0], self.start_cmd)    # used start_cmd, not bare cmd
        self.assertEqual(be.cmds[0][1], "FULL SEED")       # full seed on cold turn

    def test_warm_turn_uses_resume_cmd_and_delta(self):
        be = CmdRecordBackend([RunResult(_CODEX_JSONL, 0)])
        out = self._warm(be, session_id="th_abc123", warm_stdin="DELTA ONLY")
        used_cmd = be.cmds[0][0]
        self.assertIn("resume", used_cmd)                  # resume_cmd path
        self.assertIn("th_abc123", used_cmd)               # {session_id} substituted
        self.assertNotIn("{session_id}", used_cmd)
        self.assertEqual(be.cmds[0][1], "DELTA ONLY")      # delta, not full seed

    def test_no_stop_re_in_warm(self):
        # warm must parse full JSONL incl. turn.completed -> backend must not get stop_re
        captured = {}

        class B:
            def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
                captured["stop_re"] = stop_re
                return RunResult(_CODEX_JSONL, 0)
        self._warm(B(), session_id=None)
        self.assertIsNone(captured["stop_re"])

    def test_fallback_drops_warm_session(self):
        # primary codex quota-fails -> fallback opencode; provider != codex so the
        # caller must drop the warm session (session_ref stays None).
        be = CmdRecordBackend([RunResult("rate limit reached", 0),
                               RunResult('<<<COMPLETE nonce="x">>>', 0)])
        out = executor.run_text_with_fallback(
            "advisor", ["codex", "exec"],
            [{"provider": "opencode", "model": "k", "cmd": ["opencode", "run"]}],
            be, stdin_input="FULL SEED", cwd=str(self.proj), idle_timeout=1, hard_timeout=1,
            warm=True, session_id=None, warm_start_cmd=self.start_cmd,
            warm_resume_cmd=self.resume_cmd)
        self.assertEqual(out.provider, "opencode")
        self.assertIsNone(out.session_ref)                 # not codex -> no warm session


if __name__ == "__main__":
    unittest.main()
