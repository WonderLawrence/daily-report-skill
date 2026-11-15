"""Synthetic repositories, sessions and CLI helpers shared by regression tests."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect.py"
sys.path.insert(0, str(ROOT / "scripts"))

# The CLI layer lives in the entry script; load it by path, as it is really invoked
_spec = importlib.util.spec_from_file_location("collect_entry", SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

from daily_report import (analysis, config, evidence, gitlog,  # noqa: E402
                          pipeline, render, sessions, text, timewindow)
from daily_report.diagnostics import (GitDiagnostics, PullRequestDiagnostics,  # noqa: E402
                                      SessionDiagnostics)
from daily_report.models import (Commit, CommitRef, DirtyState, Identity,  # noqa: E402
                                   Involvement, PullRequest, Report, Session)
from daily_report.timewindow import Period  # noqa: E402


def source_sessions(name: str, period: Period,
                    diag: SessionDiagnostics | None = None) -> list[Session]:
    """Collect one source's sessions by name; test-only access to the SESSION_SOURCES registry."""
    source = next(s for s in sessions.SESSION_SOURCES if s.name == name)
    return source.collect(period, diag)

TZ = timezone(timedelta(hours=8))


def run(args, env=None, cwd=None):
    """Run the real CLI, forcing --no-prs by default.

    Tests never reach GitHub: PR discovery is always a fake response (test_prs) or patched
    in process (see `discovery`).
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    if "--prs" not in args:
        args = ["--no-prs", *args]
    return subprocess.run([sys.executable, str(SCRIPT)] + args,
                          capture_output=True, text=True, env=full_env, cwd=cwd)


def git(repo: Path, *args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, env=e, check=True)


def make_repo(base: Path, name="repo", user="Test User", email="dev@example.com") -> Path:
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", ".")
    git(repo, "config", "user.name", user)
    git(repo, "config", "user.email", email)
    return repo


def commit(repo: Path, msg: str, authored: str | None = None, committed: str | None = None,
           filename="f.txt"):
    (repo / filename).write_text(msg)
    git(repo, "add", ".")
    env = {}
    if authored:
        env["GIT_AUTHOR_DATE"] = authored
    if committed:
        env["GIT_COMMITTER_DATE"] = committed
    git(repo, "commit", "-q", "-m", msg, env=env)


START = datetime(2026, 10, 16, tzinfo=TZ)
END = START + timedelta(days=1)
PERIOD = Period(START, END, TZ)
NOON = datetime(2026, 10, 16, 12, 0, tzinfo=TZ)
IDE_BLOCK = {"type": "text", "text": "<ide_opened_file>The user opened /demo/a.rs</ide_opened_file>"}


def make_commit(subject="feat(x): a", **over) -> Commit:
    """A commit record with plausible defaults; override only what the test is about."""
    fields = {"repo": "r", "repo_path": "", "full_sha": "1", "sha": "1",
              "at": "2026-10-13T10:00:00+08:00", "authored": "2026-10-13T10:00:00+08:00",
              "author": "x", "subject": subject, "files": ["a.rs"], "added": 1, "deleted": 0}
    # files=None means there is no local copy of the commit, so the change is unknown
    fields.update(over)
    fields.setdefault("revert", fields["subject"].startswith('Revert "'))
    return Commit(**fields)


def make_session(identity="i", project="/p/demo", **over) -> Session:
    fields = {"agent": "claude-code", "project": project, "id": identity,
              "start": NOON, "end": NOON, "users": [], "concl": [],
              "tools": Counter(), "files": set(), "steps": 0}
    fields.update(over)
    return Session(**fields)


def pr_record(identity="acme/node#43", commits=(), involvement=Involvement.COMMITS,
              **over) -> PullRequest:
    """One discovery result, shaped exactly like github.discover_pull_requests output."""
    repository, number = identity.split("#")
    fields = {"id": identity, "repository": repository, "number": int(number),
              "url": f"https://github.com/{repository}/pull/{number}", "title": "Demo PR",
              "state_at_discovery": "open", "author_login": "me",
              "updated_at": "2026-10-16T10:00:00Z", "retrieved_at": "2026-10-16T12:00:00Z",
              "involvement": involvement,
              "associated_commits": [CommitRef(full_sha=c.full_sha, repo_path=c.repo_path,
                                               at=c.at, authored=c.authored, author=c.author,
                                               subject=c.subject, message=c.message,
                                               message_retrieved_at=c.message_retrieved_at) for c in commits]}
    fields.update(over)
    return PullRequest(**fields)


def discovery(prs=(), commits=(), **over):
    """Return value for patching pipeline.discover_pull_requests: (prs, commits, diagnostics)."""
    with_commits = sum(1 for pr in prs if pr.involvement == Involvement.COMMITS)
    diag = PullRequestDiagnostics(
        queries=1, prs_found=len(prs), prs_with_commits=with_commits,
        prs_without_commits=len(prs) - with_commits, checked_at="fixture")
    for key, value in over.items():
        setattr(diag, key, value)
    return list(prs), list(commits), diag


def make_report(sessions=(), commits=(), end=None, days=None, **over) -> Report:
    if end is None:
        end = START + timedelta(days=days) if days else END
    fields = {"period": Period(START, end, TZ), "commits": list(commits),
              "sessions": list(sessions)}
    fields.update(over)
    return Report(**fields)


def period_of(start, end) -> Period:
    return Period(start, end, TZ)


def make_identity(author_re=r"<(?:dev@example\.com)>\Z", **over) -> Identity:
    fields = {"author_re": author_re, "name": "Test User", "email": "dev@example.com"}
    fields.update(over)
    return Identity(**fields)


def write_jsonl(path: Path, rows, mtime: datetime = NOON) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def stderr_of(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class SessionFixture(unittest.TestCase):
    """Point sessions.HOME at a temporary directory and parse synthetic session files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._home = sessions.HOME
        sessions.HOME = self.home
        sessions.reset_project_map()

    def tearDown(self):
        sessions.HOME = self._home
        sessions.reset_project_map()
        self.tmp.cleanup()

    def claude_file(self, rows, mtime=NOON):
        return write_jsonl(self.home / ".claude" / "projects" / "-demo" / "s1.jsonl", rows, mtime)


def claude_rows():
    return [
        {"type": "user", "timestamp": "2026-10-16T10:00:00+08:00", "cwd": "/demo",
         "message": {"role": "user", "content": [IDE_BLOCK, {"type": "text", "text": "排查死锁"}]}},
        {"type": "assistant", "timestamp": "2026-10-16T10:01:00+08:00", "cwd": "/demo",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "根因是接收端锁被持有"},
             {"type": "tool_use", "name": "Read", "input": {"file_path": "/demo/a.rs"}}]}},
    ]
