"""Parley protocol: parse A's directive and B's report trailer.

ADR-07 + R2-6: nonce-fence, closer = LAST matching <<<END>>> (outermost),
exactly one directive head (else NONE/MULTIPLE). Strips ANSI (kiro output).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ATTR = re.compile(r'(\w+)="([^"]*)"')
# Delimiters tolerate 2-3 opening '<' and 1-3 closing '>': gpt-5.5 sometimes emits a
# malformed closer (e.g. `slice="...">` instead of `>>>`) — dropping repeated chars is
# a common LM slip. Anchored to the whole line + a keyword after '<<<', so code like
# `cout << x` or a stray '>' in prose can't false-match. (ADR-15 follow-up; codex#27185.)
_HEAD = re.compile(r"^[ \t]*<{2,3}(DISPATCH|PHASE|VERIFY|COMPLETE)\b([^\n<>]*)>{1,3}\s*$", re.M)
_END = re.compile(r"^[ \t]*<{2,3}END\b([^\n<>]*)>{1,3}\s*$", re.M)
_REPORT = re.compile(r"^[ \t]*<{2,3}REPORT\b([^\n<>]*)>{1,3}\s*$", re.M)


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


# Boilerplate lines CLIs print on every spawn (kiro agent-conflict warnings, trust
# banners). Pure noise that otherwise eats the whole displayed excerpt (ADR-15).
_NOISE = re.compile(
    r"^[ \t]*(?:"
    r"WARNING: Agent conflict for .*|"
    r"All tools are now trusted.*|"
    r"Agents can sometimes do unexpected.*|"
    r"WARNING: Retry #\d.*"
    r")\s*$", re.M)


def clean_excerpt(s: str) -> str:
    """Strip per-spawn CLI boilerplate for display only. Does NOT touch raw stdout
    used for parsing/classification — callers keep the raw text separately."""
    out = _NOISE.sub("", strip_ansi(s))
    # collapse the blank lines the substitution leaves behind
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _attrs(s: str) -> dict:
    return {k: v for k, v in _ATTR.findall(s)}


@dataclass
class Directive:
    kind: str  # DISPATCH | PHASE | VERIFY | COMPLETE | NONE | MULTIPLE
    role: str | None = None
    slice: str | None = None
    prompt: str | None = None
    review: str | None = None
    phase: str | None = None
    title: str | None = None
    reconciliation: str | None = None


@dataclass
class Report:
    path: str | None
    done: bool
    verdict: str | None
    excerpt: str
    raw: str


def parse(advisor_stdout: str, nonce: str) -> Directive:
    """Parse A's final directive. Requires matching nonce; exactly one head."""
    text = strip_ansi(advisor_stdout)
    valid = [(m, _attrs(m.group(2))) for m in _HEAD.finditer(text)]
    valid = [(m, a) for m, a in valid if a.get("nonce") == nonce]
    if len(valid) == 0:
        return Directive("NONE")
    if len(valid) > 1:
        return Directive("MULTIPLE")
    m, a = valid[0]
    kind = m.group(1)
    if kind == "COMPLETE":
        return Directive("COMPLETE", review=a.get("review"))
    if kind == "VERIFY":
        return Directive("VERIFY", slice=a.get("slice"), review=a.get("review"))
    if kind == "PHASE":
        return Directive("PHASE", review=a.get("review"), phase=a.get("id"), title=a.get("title"),
                         reconciliation=a.get("reconciliation"))
    # DISPATCH: body between head and the LAST matching <<<END nonce>>> (outermost, R2-6)
    ends = [e for e in _END.finditer(text)
            if _attrs(e.group(1)).get("nonce") == nonce and e.start() > m.end()]
    if not ends:
        return Directive("NONE")
    body = text[m.end():ends[-1].start()].strip("\n")
    return Directive("DISPATCH", role=a.get("role"), slice=a.get("slice"),
                     prompt=body, review=a.get("review"))


def parse_report(executor_stdout: str, excerpt_len: int = 500) -> Report:
    """Parse B's report trailer (last one wins). No trailer -> not done.
    Displayed excerpt is de-noised (clean_excerpt); raw stdout is preserved."""
    text = strip_ansi(executor_stdout)
    trailers = list(_REPORT.finditer(text))
    if not trailers:
        return Report(None, False, None, clean_excerpt(text)[:excerpt_len], text)
    t = trailers[-1]
    a = _attrs(t.group(1))
    excerpt = clean_excerpt(text[:t.start()])[:excerpt_len]
    return Report(a.get("path"), a.get("done") == "true", a.get("verdict"), excerpt, text)
