"""Git evidence: repository discovery, fetch, commit reading and worktree state.

Beyond reading, the only command run is `git fetch`: never pull, merge or rebase, and the
worktree is never touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import limits
from . import messages as m
from .diagnostics import GitDiagnostics
from .evidence import redact_text
from .models import Commit, DirtyState, FetchResult, FetchStatus, Identity
from .text import one_line
from .timewindow import Period, parse_ts

LOG_FORMAT = "%H%x1f%h%x1f%cd%x1f%ad%x1f%an <%ae>%x1f%s"


def _git(repo: Path, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kwargs)


def git_config_value(key: str) -> str:
    if not shutil.which("git"):
        return ""
    result = subprocess.run(["git", "config", "--global", key],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def author_regex(explicit: str | None, emails: list[str]) -> str | None:
    """Resolve author matching: an explicit regex wins, otherwise match whole emails.

    Git renders authors as "name <email>". Only --author accepts an arbitrary regex;
    configured emails are matched literally.
    """
    pattern = explicit
    if not pattern:
        alternatives = "|".join(re.escape(email) for email in emails if email)
        pattern = rf"<(?:{alternatives})>\Z" if alternatives else None
    if pattern:
        try:
            re.compile(pattern, re.I)
        except re.error as e:
            raise ValueError(m.AUTHOR_REGEX_INVALID.format(detail=e)) from e
    return pattern


def discover_repos(root: Path, depth: int = 2) -> list[Path]:
    found: set[Path] = set()
    root = root.expanduser()
    if not root.is_dir():
        return []
    for d in range(1, depth + 1):
        for p in root.glob("/".join(["*"] * d)):
            if p.is_dir() and (p / ".git").exists():
                found.add(p)
    return sorted(found)


# ---------------- Fetch ----------------
def _failure_detail(label: str, result: subprocess.CompletedProcess) -> str:
    """Preserve useful stderr before truncation; strip remote URLs and known secrets."""
    error = result.stderr or ""
    error = re.sub(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", "[remote-url]", error, flags=re.I)
    error = re.sub(r"[\w.+-]+@[^:\s]+:[^\s'\"<>]+", "[remote-url]", error)
    error = redact_text(error)
    lines = [line for line in error.splitlines() if line.strip()]
    excerpt = one_line(" / ".join(lines[-limits.FETCH_FAILURE_LINES:]), limits.FETCH_FAILURE)
    template = m.FETCH_DETAIL_WITH_EXCERPT if excerpt else m.FETCH_DETAIL_NO_EXCERPT
    return template.format(label=label, code=result.returncode, excerpt=excerpt)


def _fetch_one(repo: Path, enabled: bool, timeout: int, env: dict) -> FetchResult:
    """Refresh one repository's configured remotes; never merge into the working branch."""
    result = FetchResult(repo=repo.name, repo_path=str(repo))
    if not enabled:
        result.status, result.detail = FetchStatus.SKIPPED, m.FETCH_SKIPPED_DISABLED
        return result
    try:
        remotes = _git(repo, "remote", stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        if remotes.returncode:
            result.status = FetchStatus.FAILED
            result.detail = _failure_detail(m.FETCH_REMOTE_FAILED, remotes)
            return result
        result.remotes = remotes.stdout.splitlines()
        if not result.remotes:
            result.status, result.detail = FetchStatus.NO_REMOTE, m.FETCH_NO_REMOTE
            return result
        fetched = _git(repo, "fetch", "--all", "--no-recurse-submodules",
                       stdin=subprocess.DEVNULL, timeout=timeout, env=env)
        if fetched.returncode:
            result.status = FetchStatus.FAILED
            result.detail = _failure_detail(m.FETCH_FAIL_PREFIX, fetched)
        else:
            result.status, result.detail = FetchStatus.OK, m.FETCH_OK
    except subprocess.TimeoutExpired:
        result.status, result.detail = FetchStatus.TIMEOUT, m.FETCH_TIMEOUT.format(seconds=timeout)
    except (OSError, UnicodeError):
        result.status, result.detail = FetchStatus.FAILED, m.FETCH_EXEC_FAILED
    return result


def remote_urls(repo: Path, timeout: int) -> list[str]:
    """Configured fetch remote URLs; turning them into owner/repo is the github module's job."""
    result = _git(repo, "remote", "-v", timeout=timeout)
    if result.returncode:
        raise OSError(m.FETCH_READ_REMOTE_FAILED.format(repo=repo))
    return sorted({parts[1] for line in result.stdout.splitlines()
                   if len(parts := line.split()) >= 3 and parts[2] == "(fetch)"})


def fetch_repos(repos: list[Path], enabled: bool, timeout: int,
                diag: GitDiagnostics) -> list[FetchResult]:
    results = []
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for repo in repos:
        result = _fetch_one(repo, enabled, timeout, env)
        result.checked_at = datetime.now(timezone.utc).isoformat()
        if result.status in (FetchStatus.FAILED, FetchStatus.TIMEOUT):
            diag.fetch_failed += 1
            print(m.LOG_FETCH_WARN.format(name=repo.name, detail=result.detail),
                  file=sys.stderr)
        elif result.status == FetchStatus.OK:
            diag.fetch_ok += 1
        else:
            diag.fetch_skipped += 1
        results.append(result)
    return results


# ---------------- Reading commits ----------------
def _run_log(repo: Path, start, with_stats: bool) -> subprocess.CompletedProcess:
    base = ["log", "--exclude=refs/stash", "--all", "--no-merges"]
    tail = ["--date=iso-strict", f"--pretty=format:{LOG_FORMAT}"] + (["--numstat"] if with_stats else [])
    result = _git(repo, *base, f"--since-as-filter={start.isoformat()}", *tail)
    if result.returncode != 0 and "since-as-filter" in (result.stderr or ""):
        # Older Git versions lack --since-as-filter. Avoid --since here: its history
        # pruning can silently omit commits when committer dates are non-monotonic.
        print(m.LOG_FETCH_UNSUPPORTED.format(name=repo.name), file=sys.stderr)
        result = _git(repo, *base, *tail)
    return result


def _parse_log(repo: Path, output: str) -> list[Commit]:
    commits: list[Commit] = []
    for line in output.splitlines():
        if "\x1f" in line:
            full, short, committed, authored, who, subject = line.split("\x1f", 5)
            commits.append(Commit(
                repo=repo.name, repo_path=str(repo), full_sha=full, sha=short,
                at=committed, authored=authored, author=who, subject=subject,
                files=[], added=0, deleted=0, revert=subject.startswith('Revert "')))
        elif commits and line.strip():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            current = commits[-1]
            current.files.append(path)
            current.added += int(added) if added.isdigit() else 0
            current.deleted += int(deleted) if deleted.isdigit() else 0
    return commits


def git_commits(repo: Path, period: Period, diag: GitDiagnostics | None = None,
                with_stats: bool = True) -> list[Commit]:
    """Read commits inside the window, attributed by committer date."""
    result = _run_log(repo, period.start, with_stats)
    if result.returncode != 0:
        if diag is not None:
            diag.failed_files += 1
        print(m.LOG_FETCH_READ_WARN.format(repo=repo, detail=(result.stderr or '').strip()),
              file=sys.stderr)
        return []
    if diag is not None:
        diag.ok_files += 1
    if not result.stdout.strip():
        return []

    # Always apply both bounds locally, including on the --since-as-filter path.
    kept = [commit for commit in _parse_log(repo, result.stdout)
            if period.contains(parse_ts(commit.at, timezone.utc))]
    _read_revert_targets(repo, kept)
    return kept


def _read_revert_targets(repo: Path, commits: list[Commit]) -> None:
    """Read the target SHA from Git's revert body for reliable pairing."""
    for commit in commits:
        if not commit.revert:
            continue
        result = _git(repo, "show", "-s", "--format=%B", commit.full_sha)
        match = re.search(r"(?im)^This reverts commit ([0-9a-f]{7,40})\.", result.stdout)
        if result.returncode == 0 and match:
            commit.revert_target = match.group(1)


@lru_cache(maxsize=4096)
def _patch_id(repo_str: str, sha: str) -> str | None:
    show = subprocess.run(
        ["git", "-C", repo_str, "show", "--pretty=format:", "--no-ext-diff", "--binary", sha],
        capture_output=True)
    if show.returncode != 0 or not show.stdout:
        return None
    # show.stdout is bytes (--binary diffs aren't valid text), so stay in bytes mode here.
    patch = subprocess.run(["git", "patch-id", "--stable"], input=show.stdout,
                           capture_output=True)
    if patch.returncode != 0 or not patch.stdout.strip():
        return None
    return patch.stdout.decode("ascii", "replace").split()[0]


def git_patch_id(repo: Path, sha: str) -> str | None:
    """Return a stable patch identity, or None when Git cannot produce one.

    Memoized by (repo_path, sha): collapse_duplicates groups commits by (repo, subject, files)
    then calls this for each candidate; a rebased commit that lands in several PRs is otherwise
    reparsed every time.
    """
    return _patch_id(str(repo), sha)


def resolve_commits(commits: list[Commit]) -> int:
    """Fill in changed files and line counts from the local repository, by SHA.

    Commits without a local copy keep platform metadata only. Times and author are not
    overwritten: those values are already normalized to the configured timezone and are
    what the window filter used.
    """
    missing = 0
    for commit in commits:
        path = Path(commit.repo_path) if commit.repo_path else None
        result = _git(path, "show", "--numstat", "--date=iso-strict",
                      f"--pretty=format:{LOG_FORMAT}", commit.full_sha) if path else None
        parsed = _parse_log(path, result.stdout) if result and not result.returncode else []
        if not parsed:
            missing += 1
            continue
        local = parsed[0]
        commit.files, commit.added, commit.deleted = local.files, local.added, local.deleted
        if commit.revert:
            _read_revert_targets(path, [commit])
    return missing


def scan_local_commits(repos: list[Path], period: Period,
                       identity: Identity) -> tuple[set[str], dict[str, int]]:
    """SHAs of the user's local commits, plus counts of "same name, unconfigured email".

    It only answers which commits are the user's and reads no change details: the evidence
    comes from the PR traversal, this feeds coverage diagnostics alone.
    """
    shas: set[str] = set()
    alias_hits: dict[str, int] = {}
    for repo in repos:
        for commit in git_commits(repo, period, with_stats=False):
            if identity.matches_git_author(commit.author):
                shas.add(commit.full_sha)
            elif identity.same_name_other_email(commit.author):
                alias_hits[commit.author] = alias_hits.get(commit.author, 0) + 1
    return shas, alias_hits


# ---------------- Worktree state ----------------
def dirty_state(repo: Path, now: datetime) -> DirtyState | None:
    """Uncommitted changes: tracked files only, plus how many days since the last commit."""
    r = _git(repo, "status", "--porcelain=v1", "-uno")
    if r.returncode != 0:
        return None
    files = [l for l in r.stdout.splitlines() if l.strip()]
    if not files:
        return None
    last = _git(repo, "log", "-1", "--format=%ct").stdout.strip()
    age = (now - datetime.fromtimestamp(int(last), now.tzinfo)).days if last.isdigit() else None
    return DirtyState(repo=repo.name, files=len(files), age_days=age)


def collect_dirty_state(repos: list[Path], configured: bool, explicit: bool,
                        period: Period, now: datetime) -> tuple[list[DirtyState], bool]:
    period_contains_now = period.contains(now)
    enabled = explicit or (configured and period_contains_now)
    if not enabled:
        if configured:
            print(m.LOG_DIRTY_NOT_IN_WINDOW, file=sys.stderr)
        return [], False
    if explicit and not period_contains_now:
        print(m.LOG_DIRTY_FUTURE, file=sys.stderr)
    states = [state for repo in repos if (state := dirty_state(repo, now))]
    states.sort(key=lambda item: (item.age_days is None, -(item.age_days or 0)))
    return states, bool(repos)


def print_identity_report(repos: list[Path], repos_root: Path, emails: list[str],
                          identity: Identity) -> None:
    print(m.IDENTITY_USER_NAME.format(name=identity.name))
    print(m.IDENTITY_USER_EMAIL.format(email=identity.email))
    print(m.IDENTITY_EMAILS.format(emails=emails or m.IDENTITY_EMAILS_EMPTY))
    print(m.IDENTITY_REGEX.format(regex=identity.author_re))
    print(m.IDENTITY_REPOS_ROOT.format(root=repos_root, count=len(repos)) + "\n")
    for repo in repos:
        result = _git(repo, "log", "--exclude=refs/stash", "--all",
                      "--since=90 days ago", "--pretty=%an <%ae>")
        if result.returncode != 0:
            print(m.LOG_FETCH_READ_WARN.format(repo=repo, detail=(result.stderr or '').strip()),
                  file=sys.stderr)
            continue
        counts = Counter(result.stdout.splitlines())
        if not counts:
            continue
        print(f"[{repo.name}]")
        for who, count in counts.most_common(limits.IDENTITY_AUTHORS):
            mark = m.IDENTITY_MATCH_MARK if identity.matches_git_author(who) else ""
            print(f"  {count:4d}  {who}{mark}")
