import re
import unittest
from pathlib import Path

from _util import temporary_directory
from parley.backends import RunResult, Verify
from parley.channel import Channel
from parley.config import Config, Role
from parley.harness import Harness


class FakeBackend:
    """run_once: for advisor, echo scripted directive with the nonce read from the prompt.

    role_writes (optional): {rel_path: content} the backend writes during role
    execution, simulating an agent that creates the report file on disk. ADR-15
    stale-protection only accepts a no-trailer report file if it's written DURING
    the run, so tests must write it here rather than pre-creating it as a fixture.
    """
    def __init__(self, advisor_scripts, role_output, role_writes=None):
        self.advisor_scripts = advisor_scripts
        self.role_output = role_output
        self.role_writes = role_writes or {}
        self.i = 0
        self.role_prompts = []

    def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
        if profile.name == "advisor":
            m = re.search(r'nonce="([0-9a-fA-F]+)"', prompt)
            nonce = m.group(1) if m else "x"
            out = self.advisor_scripts[self.i].format(N=nonce)
            self.i += 1
            return RunResult(out, 0)
        self.role_prompts.append(prompt)
        for rel, content in self.role_writes.items():
            dst = Path(cwd) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
        # LS-021: a real role that emits a trailer also WRITES the report file. Parse
        # REPORT_PATH from the role prompt and materialize it so the trailer is backed
        # by a real file (else classify() -> MISSING_REPORT and the harness can't proceed).
        if "<<<REPORT" in (self.role_output or ""):
            m = re.search(r"REPORT_PATH:\s*(\S+)", prompt)
            if m:
                dst = Path(cwd) / m.group(1)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    dst.write_text("# report\nwork done\n", encoding="utf-8")
        return RunResult(self.role_output, 0)


class FakeVerifyRunner:
    def run(self, gates, cwd, timeout):
        return Verify(0, None, "all gates pass")


class TestHarnessLoop(unittest.TestCase):
    def test_full_slice_to_complete_with_commit(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        (proj / "docs" / "reports").mkdir(parents=True)
        rpath = "docs/reports/x.md"
        (proj / rpath).write_text("# coder report\nimplemented", encoding="utf-8")

        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)})
        ch = Channel(cfg.data_dir)

        scripts = [
            '<<<DISPATCH nonce="{N}" role="coder" slice="s1">>>\nbuild it\n<<<END nonce="{N}">>>',
            '<<<VERIFY nonce="{N}" slice="s1" review="APPROVE">>>',
            '<<<COMPLETE nonce="{N}">>>',
        ]
        report = f'## done\nimplemented\n<<<REPORT path="{rpath}" done="true" verdict="APPROVE">>>'
        backend = FakeBackend(scripts, report)

        commits = []
        def fake_commit(pd, sl, msg=None):
            commits.append(sl)
            return "sha123"

        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}', git_commit=fake_commit)
        reason = h.run(goal="build feature", contract="C", first_phase=12)

        self.assertEqual(reason, "done")
        types = [e["type"] for e in ch.tail(100)]
        self.assertIn("dispatch", types)
        self.assertIn("advisor", types)
        self.assertIn("report", types)
        self.assertIn("verify", types)
        self.assertIn("slice_done", types)
        self.assertEqual(ch.tail(1)[0]["type"], "stopped")
        self.assertEqual(ch.tail(1)[0]["reason"], "done")
        sd = [e for e in ch.tail(100) if e["type"] == "slice_done"][0]
        self.assertEqual((sd["slice"], sd["commit"]), ("s1", "sha123"))
        self.assertEqual(commits, ["s1"])               # committed exactly once (R2-9)
        rep = [e for e in ch.tail(100) if e["type"] == "report"][0]
        self.assertIsNotNone(rep["snapshot_path"])      # ADR-05 snapshot taken
        events = ch.tail(100)
        report_i = next(i for i, e in enumerate(events) if e["type"] == "report")
        advisor_i = next(i for i, e in enumerate(events[report_i + 1:], report_i + 1)
                         if e["type"] == "advisor")
        self.assertEqual(events[advisor_i]["verdict"], "APPROVE")
        self.assertEqual(events[advisor_i]["action"], "VERIFY")
        dispatch = next(e for e in events if e["type"] == "dispatch")
        self.assertTrue(Path(dispatch["prompt_path"]).is_file())
        self.assertIn("build it", Path(dispatch["prompt_path"]).read_text(encoding="utf-8"))
        self.assertIn("PROMPT_PATH:", backend.role_prompts[0])
        self.assertNotIn("build it", backend.role_prompts[0])

    def test_report_file_fallback_when_no_trailer(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        proj.mkdir(parents=True)
        # B "writes" the report at the deterministic path DURING the run (ADR-15
        # stale-protection rejects a pre-existing file); harness accepts it via backstop.
        rp = "docs/reports/phase12-slice-s1-coder-report.md"
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)})
        ch = Channel(cfg.data_dir)
        scripts = ['<<<DISPATCH nonce="{N}" role="coder" slice="s1">>>\ngo\n<<<END nonce="{N}">>>',
                   '<<<COMPLETE nonce="{N}" review="APPROVE">>>']
        # no <<<REPORT>>> trailer, but writes the report file during execution
        backend = FakeBackend(scripts, "did the work, wrote the file",
                              role_writes={rp: "# coder report (no trailer)"})
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}')
        self.assertEqual(h.run("g", "c", 12), "done")
        rep = [e for e in ch.tail(100) if e["type"] == "report"][0]
        self.assertEqual(rep["report_path"], rp)        # linked via fallback
        self.assertTrue(rep["done"])
        self.assertIsNotNone(rep["snapshot_path"])       # snapshotted

    def test_executor_stuck_stops_clean_no_advisor_review(self):
        # ADR-15: when the executor chain is exhausted (rate-limit with no trailer
        # and no fallbacks), the harness must stop("executor_stuck") and NEVER send
        # the failure to the Advisor as a done=false report (the old quota-burn loop).
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        (proj / "docs" / "reports").mkdir(parents=True)
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)})   # no fallbacks
        ch = Channel(cfg.data_dir)
        scripts = ['<<<DISPATCH nonce="{N}" role="coder" slice="s1">>>\ngo\n<<<END nonce="{N}">>>',
                   '<<<COMPLETE nonce="{N}" review="APPROVE">>>']
        # executor emits a rate-limit, never writes a report file, no trailer
        backend = FakeBackend(scripts, "⚠️ Kiro rate limit reached\nRetry #2, retrying within 10.0s..")
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}')
        self.assertEqual(h.run("g", "c", 1), "executor_stuck")
        events = ch.tail(100)
        # ADR-15 key guarantee: the executor failure is NEVER turned into a report
        # (the old quota-burn loop fed done=false to the Advisor as a report).
        self.assertFalse(any(e["type"] == "report" for e in events))
        # the only advisor event is the initial DISPATCH directive; no review of a
        # failed report happened (no second advisor event after the dispatch).
        advisors = [e for e in events if e["type"] == "advisor"]
        self.assertEqual(len(advisors), 1)
        self.assertEqual(advisors[0]["action"], "DISPATCH")
        # executor_exhausted emitted, then stopped(executor_stuck)
        exh = [e for e in events if e["type"] == "executor_exhausted"]
        self.assertEqual(len(exh), 1)
        self.assertEqual(exh[0]["reason"], "rate_limited")
        self.assertEqual([e for e in events if e["type"] == "stopped"][0]["reason"], "executor_stuck")

    def test_report_requires_advisor_review(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        rp = "docs/reports/phase1-slice-s1-coder-report.md"
        (proj / "docs" / "reports").mkdir(parents=True)
        (proj / rp).write_text("# report", encoding="utf-8")
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)})
        ch = Channel(cfg.data_dir)
        scripts = [
            '<<<DISPATCH nonce="{N}" role="coder" slice="s1">>>\ngo\n<<<END nonce="{N}">>>',
            '<<<COMPLETE nonce="{N}">>>',
            '<<<COMPLETE nonce="{N}" review="APPROVE">>>',
        ]
        backend = FakeBackend(scripts, f'<<<REPORT path="{rp}" done="true">>>')
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}')
        self.assertEqual(h.run("g", "c", 1), "done")
        errors = [e for e in ch.tail(100) if e["type"] == "turn_error"]
        self.assertEqual(errors[0]["reason"], "advisor_missing_report_review")

    def test_advisor_approve_commits_readonly_slice(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        rp = "docs/reports/phase1-slice-s1-analyzer-report.md"
        (proj / "docs" / "reports").mkdir(parents=True)
        (proj / rp).write_text("# analysis", encoding="utf-8")
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"analyzer": Role(cmd=["x"], edit=False)})
        ch = Channel(cfg.data_dir)
        scripts = ['<<<DISPATCH nonce="{N}" role="analyzer" slice="s1">>>\ninspect\n<<<END nonce="{N}">>>',
                   '<<<COMPLETE nonce="{N}" review="APPROVE">>>']
        backend = FakeBackend(scripts, f'<<<REPORT path="{rp}" done="true">>>')
        commits = []
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}',
                    git_commit=lambda pd, sl, msg=None: (commits.append(sl) or "shaRO"))
        self.assertEqual(h.run("g", "c", 1), "done")
        self.assertEqual(commits, ["s1"])   # ADR-11: read-only slice commit khi advisor APPROVE (no verify)
        self.assertTrue(any(e["type"] == "slice_done" for e in ch.tail(100)))

    def test_push_on_done_when_enabled(self):
        from parley.config import Git
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(project_dir=tmp.name, data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)}, git=Git(auto_push=True))
        ch = Channel(cfg.data_dir)
        backend = FakeBackend(['<<<COMPLETE nonce="{N}">>>'], "")
        pushes = []
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}',
                    git_push=lambda pd, br, rem: (pushes.append(br) or True))
        self.assertEqual(h.run("g", "c", 1), "done")
        self.assertEqual(pushes, ["parley/work"])      # ADR-11: push once on successful done
        self.assertTrue(any(e["type"] == "push" and e["ok"] for e in ch.tail(100)))

    def test_observer_stop_at_top(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        cfg = Config(project_dir=tmp.name, data_dir=str(Path(tmp.name) / "data"),
                     roles={"coder": Role(cmd=["x"], edit=True)})
        ch = Channel(cfg.data_dir)
        (Path(cfg.data_dir) / "control.json").write_text('{"seq":1,"verdict":"stop"}', encoding="utf-8")
        backend = FakeBackend(['<<<COMPLETE nonce="{N}">>>'], "")
        h = Harness(cfg, ch, backend, FakeVerifyRunner(), sup_runner=lambda p: '{"verdict":"continue"}')
        self.assertEqual(h.run("g", "c", 1), "observer")
        self.assertEqual(backend.i, 0)                   # A never called (stop before A)


class WarmJsonlBackend:
    """Advisor backend that emits codex-style JSONL (thread_id + turn.completed) so the
    harness can resolve a warm session. Records the (cmd, stdin) of each advisor call to
    let tests assert cold-seed (turn 1) vs delta-resume (turn 2+). Roles return a report."""
    def __init__(self, advisor_scripts, role_output, thread_id="th-1"):
        self.advisor_scripts = advisor_scripts
        self.role_output = role_output
        self.thread_id = thread_id
        self.i = 0
        self.advisor_calls = []   # (cmd, stdin) per advisor turn

    def run_once(self, profile, prompt, cwd, idle_timeout, hard_timeout, stop_re=None):
        if profile.name == "advisor":
            self.advisor_calls.append((list(profile.cmd), prompt))
            m = re.search(r'nonce="([0-9a-fA-F]+)"', prompt)
            nonce = m.group(1) if m else "x"
            directive = self.advisor_scripts[self.i].format(N=nonce)
            self.i += 1
            # codex --json stream: thread_id event, the directive as an item, turn.completed
            out = (f'{{"thread_id":"{self.thread_id}"}}\n'
                   f'{{"type":"item.completed","item":{{"text":{__import__("json").dumps(directive)}}}}}\n'
                   f'{{"type":"turn.completed"}}')
            return RunResult(out, 0)
        return RunResult(self.role_output, 0)


class TestWarmAdvisor(unittest.TestCase):
    def test_warm_session_kept_within_phase_then_reset_on_phase(self):
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        rp = "docs/reports/phase1-slice-s1-analyzer-report.md"
        (proj / "docs" / "reports").mkdir(parents=True)
        (proj / rp).write_text("# analysis", encoding="utf-8")
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"analyzer": Role(cmd=["x"], edit=False)})
        ch = Channel(cfg.data_dir)
        # turn1: dispatch (cold seed) -> turn2: APPROVE review (warm delta) ->
        # turn3: PHASE (force cold) -> turn4: COMPLETE (cold seed again)
        scripts = [
            '<<<DISPATCH nonce="{N}" role="analyzer" slice="s1">>>\ngo\n<<<END nonce="{N}">>>',
            '<<<PHASE nonce="{N}" id="2" review="APPROVE">>>',
            '<<<COMPLETE nonce="{N}" review="APPROVE">>>',
        ]
        backend = WarmJsonlBackend(scripts, f'<<<REPORT path="{rp}" done="true">>>')
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}',
                    git_commit=lambda pd, sl, msg=None: "sha")
        self.assertEqual(h.run("goal text here", "CONTRACT", 1), "done")

        calls = backend.advisor_calls
        self.assertGreaterEqual(len(calls), 3)
        # turn 1: cold -> start_cmd (codex exec --json, NOT resume) + full seed (has GOAL/CONTRACT)
        cmd1, stdin1 = calls[0]
        self.assertIn("--json", cmd1)
        self.assertNotIn("resume", cmd1)
        self.assertIn("goal text here", stdin1)          # full seed carries the goal
        # turn 2: warm -> resume_cmd with the thread id, delta stdin (NO full goal header)
        cmd2, stdin2 = calls[1]
        self.assertIn("resume", cmd2)
        self.assertIn("th-1", cmd2)
        self.assertNotIn("goal text here", stdin2)        # delta omits the seed header
        # turn 3 (right after PHASE): cold again -> start_cmd, full seed
        cmd3, stdin3 = calls[2]
        self.assertNotIn("resume", cmd3)
        self.assertIn("goal text here", stdin3)

    def test_force_cold_after_max_warm_turns(self):
        # ADR-14 point 5: when max_warm_turns_per_phase is hit, the next turn drops the
        # session and re-seeds with FULL context (a_ctx), never a delta — a fresh codex
        # session must carry goal/contract/policy again.
        from parley.config import Limits
        tmp = temporary_directory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        rp = "docs/reports/phase1-slice-s1-analyzer-report.md"
        (proj / "docs" / "reports").mkdir(parents=True)
        (proj / rp).write_text("# analysis", encoding="utf-8")
        cfg = Config(project_dir=str(proj), data_dir=str(Path(tmp.name) / "data"),
                     roles={"analyzer": Role(cmd=["x"], edit=False)},
                     limits=Limits(max_warm_turns_per_phase=2))   # force cold after 2 warm turns
        ch = Channel(cfg.data_dir)
        # t1 dispatch (cold) -> t2 warm delta (review) -> t3 must FORCE-COLD (>=1 warm) ->
        # t4 COMPLETE. Use REJECT then re-dispatch to spend turns without changing phase.
        scripts = [
            '<<<DISPATCH nonce="{N}" role="analyzer" slice="s1">>>\ngo\n<<<END nonce="{N}">>>',
            '<<<VERIFY nonce="{N}" slice="s1" review="APPROVE">>>',
            '<<<VERIFY nonce="{N}" slice="s1">>>',
            '<<<COMPLETE nonce="{N}">>>',
        ]
        backend = WarmJsonlBackend(scripts, f'<<<REPORT path="{rp}" done="true">>>')
        h = Harness(cfg, ch, backend, FakeVerifyRunner(),
                    sup_runner=lambda p: '{"verdict":"continue"}',
                    git_commit=lambda pd, sl, msg=None: "sha")
        self.assertEqual(h.run("goal text here", "CONTRACT", 1), "done")

        calls = backend.advisor_calls
        # t1 cold, t2 warm (resume+delta). t3: warm_turns hit 1 -> force cold: start_cmd + full seed.
        cmd2, stdin2 = calls[1]
        self.assertIn("resume", cmd2)                     # t2 was warm
        cmd3, stdin3 = calls[2]
        self.assertNotIn("resume", cmd3)                  # t3 forced cold (not resume)
        self.assertIn("goal text here", stdin3)           # ...with FULL context, not a delta


if __name__ == "__main__":
    unittest.main()
