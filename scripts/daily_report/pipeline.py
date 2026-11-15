"""Collection orchestration: PR traversal first, then sessions and worktree
state.

PRs are the index of the Git evidence. The first level searches PRs updated in
the window
that involve the user; the second keeps the commits inside those PRs that are
the user's
and whose committer date falls in the window; local repositories then fill in
changed files
and line counts by SHA. The local scan only feeds one diagnostic line ("N of
your commits
belong to no PR of this period") and never enters the evidence.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime

from . import messages as m
from .analysis import collapse_duplicates, pair_reverts
from .diagnostics import (
    CoverageStatus,
    GitDiagnostics,
    PullRequestDiagnostics,
    SourceDiagnostics,
)
from .github import discover_pull_requests
from .gitlog import (
    collect_dirty_state,
    fetch_repos,
    resolve_commits,
    scan_local_commits,
)
from .models import Commit, FetchResult, PullRequest, Report, Scope
from .sessions import collect_sessions
from .settings import Settings


@dataclass
class GitEvidence:
    """Git side result, returned together with its two diagnostics rather than
    via an out parameter.
    """

    fetch_results: list[FetchResult] = field(default_factory=list)
    pull_requests: list[PullRequest] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)
    extras: int = 0
    diagnostics: GitDiagnostics = field(default_factory=GitDiagnostics)
    pr_diagnostics: PullRequestDiagnostics = field(default_factory=PullRequestDiagnostics)


def _warn_alias_identities(alias_hits: dict[str, int], name: str) -> None:
    """Commits sharing user.name but with an unconfigured email: the real
    missing identity signal.
    """
    if not alias_hits:
        return
    detail = ", ".join(
        m.LOG_ALIAS_ENTRY.format(who=who, count=count) for who, count in sorted(alias_hits.items(), key=lambda item: -item[1])
    )
    print(m.LOG_ALIAS_IDENTITIES.format(name=name, detail=detail), file=sys.stderr)


def _count_local_only(settings: Settings, found: set[str], diag: GitDiagnostics) -> None:
    """The local scan answers one question: how many of the user's commits
    reached no PR.
    """
    if not settings.git.enabled:
        return
    shas, alias_hits = scan_local_commits(settings.repos, settings.period, settings.identity)
    _warn_alias_identities(alias_hits, settings.identity.name)
    diag.local_commits = len(shas)
    diag.local_unassociated = len(shas - found)
    if diag.local_unassociated:
        print(
            m.LOG_LOCAL_UNASSOCIATED.format(count=diag.local_unassociated),
            file=sys.stderr,
        )


def collect_git(settings: Settings) -> GitEvidence:
    diag = GitDiagnostics(
        scanned=len(settings.repos),
        disabled=not settings.git.enabled,
        unavailable=settings.git.enabled and not settings.repos,
    )
    fetch_results = fetch_repos(settings.repos, settings.git.fetch, settings.git.fetch_timeout, diag)
    pull_requests, commits, pr_diagnostics = discover_pull_requests(
        settings.period,
        settings.identity,
        settings.repo_index,
        scoped=settings.scoped,
        enabled=settings.github.enabled,
        timeout=settings.github.timeout,
        max_queries=settings.github.max_queries,
    )
    if pr_diagnostics.status in (
        CoverageStatus.PARTIAL,
        CoverageStatus.UNAVAILABLE,
    ):
        print(m.LOG_PR_COVERAGE_PARTIAL, file=sys.stderr)

    diag.commits_without_local_copy = resolve_commits(commits)
    pair_reverts(commits)
    shown, extras = collapse_duplicates(commits)
    diag.commits = len(shown)
    if extras:
        print(m.LOG_PATCH_ID_COLLAPSED.format(count=extras), file=sys.stderr)
    _count_local_only(settings, {commit.full_sha for commit in commits}, diag)
    return GitEvidence(fetch_results, pull_requests, shown, extras, diag, pr_diagnostics)


def run_collection(settings: Settings) -> Report:
    git = collect_git(settings)
    diagnostics: dict[str, SourceDiagnostics] = {"git": git.diagnostics}
    sessions, truncated = collect_sessions(
        settings.period,
        settings.sessions.sources,
        git.commits,
        settings.sessions.limit,
        diagnostics,
        settings.project_roots,
    )

    now = datetime.now(settings.period.tz)
    dirty, dirty_scanned = collect_dirty_state(
        settings.repos,
        settings.dirty_configured,
        settings.dirty_explicit,
        settings.period,
        now,
    )
    scope = Scope(
        git=settings.git.enabled,
        repos=sorted({path.name for path in settings.repos}) if settings.git.enabled else [],
        sessions=sorted(settings.sessions.sources),
        dirty=dirty_scanned,
        only_repos=[str(path) for path in settings.project_roots or []],
    )
    return Report(
        period=settings.period,
        commits=git.commits,
        sessions=sessions,
        truncated=truncated,
        extras=git.extras,
        dirty=dirty,
        fetch_results=git.fetch_results,
        pull_requests=git.pull_requests,
        pr_diagnostics=git.pr_diagnostics,
        diagnostics=diagnostics,
        scope=scope,
        stale_days=settings.stale_days,
        generated=now,
        period_word=settings.period_word,
    )
