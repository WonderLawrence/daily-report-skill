"""Scan and parse Claude Code / Codex / DSH session files.

Candidates are prefiltered by directory and mtime, and only events inside the window are
kept; one damaged file never stops the other sources. A new agent is one more entry in
SESSION_SOURCES.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .analysis import link_sessions, mark_near_duplicates, select_session_details
from .diagnostics import SessionDiagnostics, SourceDiagnostics
from .models import Commit, Event, Session
from .session_events import record_cwd, record_events, record_timestamp
from .text import one_line
from .timewindow import Period

HOME = Path.home()

TOOL_PATH_KEYS = ("file_path", "path", "notebook_path")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


_PROJECT_MAP: dict[str, str] | None = None


def claude_project_map() -> dict[str, str]:
    """Slug directory -> real cwd (subagent files in the same directory often lack cwd)."""
    global _PROJECT_MAP
    if _PROJECT_MAP is not None:
        return _PROJECT_MAP
    _PROJECT_MAP = {}
    base = HOME / ".claude" / "projects"
    for f in base.glob("*/*.jsonl"):
        if f.parent.name in _PROJECT_MAP:
            continue
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(d, dict) and isinstance(d.get("cwd"), str):
                        _PROJECT_MAP[f.parent.name] = d["cwd"]
                        break
        except OSError:
            continue
    return _PROJECT_MAP


def reset_project_map() -> None:
    global _PROJECT_MAP
    _PROJECT_MAP = None


# ---------------- Source registry ----------------
def _claude_project(path: Path) -> str:
    return claude_project_map().get(path.parent.name, path.parent.name)


def _codex_project(path: Path) -> str:
    return path.stem


def _dsh_project(path: Path) -> str:
    return path.parent.parent.name.strip("-").replace("-", "/")


@dataclass(frozen=True)
class AgentSource:
    """One session source: diagnostics name, record format, directory under HOME, pattern."""

    name: str                        # Source name used in diagnostics: claude / codex / dsh
    agent: str                       # Record format: claude-code / codex / dsh
    parts: tuple[str, ...]           # Directory under HOME, resolved late because tests swap HOME
    pattern: str
    project_fallback: Callable[[Path], str]
    requires: str | None = None      # External command this source needs
    id_from_parent: bool = False     # A DSH session may span several files in one directory

    @property
    def location(self) -> str:
        return "~/" + "/".join(self.parts)

    def base(self) -> Path:
        return HOME.joinpath(*self.parts)

    def session_id(self, path: Path) -> str:
        return path.parent.name if self.id_from_parent else path.stem

    def collect(self, period: Period, diag: SessionDiagnostics | None = None) -> list[Session]:
        return scan_sessions(self, period, diag)


SESSION_SOURCES = (
    AgentSource("claude", "claude-code", (".claude", "projects"), "*/*.jsonl", _claude_project),
    AgentSource("codex", "codex", (".codex", "sessions"), "*/*/*/*.jsonl", _codex_project),
    AgentSource("dsh", "dsh", (".dsh", "sessions"), "*/session-*/*.jsonl.zstd", _dsh_project,
                requires="zstd", id_from_parent=True),
)


# ---------------- Parsing one file ----------------
def _scan_candidate(path: Path, period: Period, diag: SessionDiagnostics) -> bool:
    """Prefilter by mtime; copied files with unreliable mtimes may need manual review."""
    mtime = _mtime(path)
    diag.discovered += 1
    if mtime is None:
        diag.scanned += 1
        diag.failed_files += 1
        return False
    if mtime < period.start.timestamp() - 86400:
        return False
    diag.scanned += 1
    if period.start.timestamp() <= mtime < period.end.timestamp():
        diag.files += 1
    return True


def session_records(path: Path, counters: SessionDiagnostics):
    """Yield original line numbers; one damaged file must not stop other sources."""
    proc = None
    try:
        if path.suffix == ".zstd":
            # Stream so a large DSH log doesn't load whole into memory before we see the first line
            # stderr goes to the void on purpose: nothing reads it, and an undrained pipe
            # deadlocks once a corrupt archive fills the buffer. The exit code is the signal.
            proc = subprocess.Popen(["zstd", "-dc", str(path)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, text=True,
                                    encoding="utf-8", errors="replace")
            stream = proc.stdout
        else:
            stream = path.open(encoding="utf-8", errors="replace")
        try:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("record must be an object")
                except (ValueError, TypeError):
                    counters.invalid_records += 1
                    continue
                yield line_no, row
        finally:
            stream.close()
            if proc is not None and proc.wait() != 0:
                counters.read_errors += 1
    except (OSError, UnicodeError):
        counters.read_errors += 1
        if proc is not None:
            proc.kill()
            proc.wait()


def _tool_paths(event: Event) -> list[str]:
    """File paths in tool call arguments, used for the weak link to commits."""
    args = json.loads(event.text).get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []
    return [args[key] for key in TOOL_PATH_KEYS if isinstance(args.get(key), str)]


@dataclass
class _Draft:
    """Accumulate a session summary event by event; summaries are lossy, events are not."""

    events: list[Event] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    concl: list[str] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    files: set[str] = field(default_factory=set)
    first: datetime | None = None
    last: datetime | None = None

    def observe(self, ts: datetime) -> None:
        self.first = min(self.first, ts) if self.first else ts
        self.last = max(self.last, ts) if self.last else ts

    def add(self, event: Event) -> None:
        self.events.append(event)
        if event.kind == "user_request":
            self.users.append(one_line(event.text, 220))
        elif event.kind == "assistant_statement":
            self.concl.append(one_line(event.text, 260))
        elif event.kind == "tool_call":
            self.tools[json.loads(event.text).get("name") or "?"] += 1
            self.files.update(_tool_paths(event))


def parse_session_file(path: Path, agent: str, period: Period, diag: SessionDiagnostics,
                       fallback: str, session_id: str | None = None) -> Session | None:
    local = SessionDiagnostics()
    draft = _Draft()
    cwd = None
    for line_no, row in session_records(path, local):
        cwd = cwd or record_cwd(row, agent)
        ts = record_timestamp(row, agent, period.tz)
        if ts is None:
            try:
                probe = SessionDiagnostics()
                extracted = record_events(row, agent, probe)
                local.unparsed_messages += probe.unparsed_messages
                if extracted or probe.unparsed_messages:
                    local.invalid_records += 1
            except (TypeError, AttributeError, ValueError):
                local.invalid_records += 1
            continue
        local.timestamps += 1
        if not period.contains(ts):
            continue
        local.events += 1
        draft.observe(ts)
        try:
            extracted = record_events(row, agent, local)
        except (TypeError, AttributeError, ValueError):
            local.invalid_records += 1
            continue
        for event in extracted:
            event.at, event.source, event.line = ts, str(path), line_no
            draft.add(event)
    diag.merge(local)
    unknown = not local.timestamps or (local.events and not draft.events)
    if local.read_errors or local.invalid_records or local.unparsed_messages or unknown:
        diag.failed_files += 1
        if unknown and not local.read_errors:
            diag.unrecognized_files += 1
    else:
        diag.ok_files += 1
    if not draft.events:
        return None
    return Session(
        agent=agent, project=cwd if isinstance(cwd, str) and cwd else fallback,
        id=session_id if session_id is not None else path.stem,
        start=draft.first, end=draft.last, users=draft.users, concl=draft.concl[-3:],
        tools=draft.tools, files=draft.files, steps=sum(draft.tools.values()),
        events=draft.events, source=str(path))


def scan_sessions(source: AgentSource, period: Period,
                  diag: SessionDiagnostics | None = None) -> list[Session]:
    diag = diag if diag is not None else SessionDiagnostics()
    base = source.base()
    if not base.is_dir():
        diag.unavailable = True
        return []
    if source.requires and not shutil.which(source.requires):
        diag.unavailable = True
        print(f"[note] 未找到 {source.requires}，跳过 {source.name.upper()} 会话"
              f"（brew install {source.requires}）", file=sys.stderr)
        return []
    found = []
    for path in sorted(base.glob(source.pattern)):
        if not _scan_candidate(path, period, diag):
            continue
        session = parse_session_file(path, source.agent, period, diag,
                                     source.project_fallback(path), source.session_id(path))
        if session:
            found.append(session)
    return found


# ---------------- Aggregation ----------------
def warn_format_drift(name: str, location: str, diag: SessionDiagnostics, found: int) -> None:
    """A changed session format silently yields zero sessions.

    Detect it as "files touched but no events" or "events parsed but nothing extracted".
    """
    if diag.unparsed_messages:
        format_hint = "；Codex 文字仅支持 response_item/message" if name == "codex" else ""
        print(f"[warn] {name}: {diag.unparsed_messages} 条文字消息未能完整解析；"
              f"格式不支持或内容损坏，检查 {location}{format_hint}。已保留可读内容及工具记录", file=sys.stderr)
    if diag.files and not diag.events:
        print(f"[warn] {name}: 窗口内有 {diag.files} 个会话文件被修改，但没解析出任何事件；"
              f"会话格式可能变了，检查 {location}。这不代表当天没有会话", file=sys.stderr)
    elif diag.events and not found:
        print(f"[warn] {name}: 解析到 {diag.events} 条事件，但没提取出任何用户消息或工具调用；"
              f"会话格式可能变了，检查 {location}", file=sys.stderr)


def project_in_scope(project: str, roots: list[Path]) -> bool | None:
    """None means the parser did not recover an absolute cwd; never match by basename."""
    path = Path(project).expanduser()
    if not path.is_absolute():
        return None
    path = path.resolve()
    return any(path == root or root in path.parents for root in roots)


def _merge_fragments(sessions: list[Session]) -> list[Session]:
    """A DSH session may be split across files. Keep all fragments and their locators."""
    by_id: dict[tuple, Session] = {}
    for session in sessions:
        key = (session.agent, session.id)
        kept = by_id.get(key)
        if kept is None:
            by_id[key] = session
            continue
        kept.start = min(kept.start, session.start)
        kept.end = max(kept.end, session.end)
        kept.events = sorted(kept.events + session.events,
                             key=lambda event: (event.at, event.source, event.line))
        kept.users = [one_line(event.text, 220) for event in kept.events
                      if event.kind == "user_request"]
        kept.concl = [one_line(event.text, 260) for event in kept.events
                      if event.kind == "assistant_statement"][-3:]
        kept.tools.update(session.tools)
        kept.files.update(session.files)
        kept.steps += session.steps
    return sorted(by_id.values(), key=lambda session: (session.start, session.id))


def collect_sessions(period: Period, enabled: set[str], commits: list[Commit], limit: int,
                     diagnostics: dict[str, SourceDiagnostics] | None = None,
                     project_roots: list[Path] | None = None) -> tuple[list[Session], bool]:
    """Collect sessions per source; out of scope or unidentifiable ones only feed diagnostics."""
    diagnostics = diagnostics if diagnostics is not None else {}
    sessions: list[Session] = []
    for source in SESSION_SOURCES:
        diag = SessionDiagnostics()
        diagnostics[source.name] = diag
        if source.name not in enabled:
            diag.disabled = True
            continue
        found = source.collect(period, diag)
        diag.sessions = len(found)
        warn_format_drift(source.name, source.location, diag, len(found))
        if diag.failed_files:
            print(f"[warn] {source.name}: {diag.failed_files} 个文件读取或解析不完整；"
                  "详见证据包采集诊断", file=sys.stderr)
        for session in found:
            included = (project_in_scope(session.project, project_roots)
                        if project_roots is not None else True)
            if included:
                sessions.append(session)
            elif included is None:
                diag.scope_unknown += 1
            else:
                diag.scope_excluded += 1

    sessions = _merge_fragments(sessions)
    mark_near_duplicates(sessions)
    link_sessions(sessions, commits)
    select_session_details(sessions, limit)
    by_agent = {source.agent: diagnostics[source.name] for source in SESSION_SOURCES}
    for diag in by_agent.values():
        diag.sessions = 0
    for session in sessions:
        diag = by_agent[session.agent]
        diag.sessions += 1
        if session.detailed:
            diag.detailed += 1
        else:
            diag.omitted += 1
    return sessions, len(sessions) > limit
