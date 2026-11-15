"""Render the collection result into the Markdown evidence pack an agent reads.

One function per section, each returning lines. The wording states the evidence and the
known gaps only; it never decides for the user what counts as an achievement.
Output strings stay in Chinese: they are the deliverable, and the skill documents refer to
these exact section titles.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import limits
from . import messages as m
from .analysis import scope_summary
from .diagnostics import (GitDiagnostics, PullRequestDiagnostics, SessionDiagnostics,
                          SourceDiagnostics)
from .evidence import format_counts
from .models import (Commit, DirtyState, Event, EventKind, FetchResult, Involvement,
                     PullRequest, Report, Scope, Session)
from .text import one_line, short_id, substantive_text

# One place for the four Involvement values and their rendered descriptions; keeps the
# contract between discovery (writer) and this section (reader) in a single table.
_PR_STATE: dict[str, str] = {
    Involvement.COMMITS: m.R_PR_STATE_COMMITS,
    Involvement.NO_PERIOD_COMMITS: m.R_PR_STATE_NO_COMMITS,
    Involvement.UNQUERIED: m.R_PR_STATE_UNQUERIED,
    Involvement.FAILED: m.R_PR_STATE_FAILED,
}


def _rel(paths: set[str], cwd: str, limit: int = limits.REL_PATHS) -> list[str]:
    out = []
    for p in sorted(paths):
        try:
            p = str(Path(p).relative_to(cwd))
        except (ValueError, TypeError):
            pass
        if len(p) < 90:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _period_word(days: int) -> str:
    if days == 1:
        return m.R_PERIOD_DAILY
    if days == 7:
        return m.R_PERIOD_WEEKLY
    return m.R_PERIOD_GENERIC.format(days=days)


def coverage_status(diag: SourceDiagnostics) -> str:
    """Turn one source's counts into a sentence; a missing source and "no records" differ."""
    if diag.disabled:
        return m.COV_DISABLED
    if diag.unavailable:
        return m.COV_UNAVAILABLE
    if diag.failed_files:
        return m.COV_PARTIAL_SOME_OK if diag.ok_files else m.COV_FAILED
    if diag.has_fetch_gap:
        return m.COV_FETCH_GAP
    return m.COV_OK if diag.has_records else m.COV_EMPTY


def event_excerpt(event: Event, limit: int = limits.EVENT_EXCERPT) -> str:
    locator = f"{event.at:%m-%d %H:%M:%S} · {event.source}:{event.line}"
    call = m.R_EVENT_CALL_SUFFIX.format(call_id=event.call_id) if event.call_id else ""
    return m.R_EVENT_LOCATOR.format(text=one_line(event.text, limit), locator=locator, call=call)


def session_index_hint(session: Session) -> tuple[str, str | None]:
    """Find a usable text clue, without deciding work value or dropping evidence."""

    requests = [text for text in session.users if substantive_text(text)]
    if requests:
        chosen = requests[:1] + requests[-1:] if len(requests) > 1 else requests
        return one_line(" / ".join(chosen), limits.SESSION_HINT), EventKind.USER_REQUEST
    # A continuation may have no in-window request, or end with an acknowledgment.
    # Use the last substantive statement in the retained in-window events.
    claims = [event.text for event in session.events
              if event.kind == EventKind.ASSISTANT_STATEMENT] or session.concl
    for text in reversed(claims):
        if substantive_text(text):
            return one_line(text, limits.SESSION_HINT), EventKind.ASSISTANT_STATEMENT
    return "", None


# ---------------- Sections ----------------
def _header(report: Report) -> list[str]:
    period = report.period
    span = (period.start.strftime("%Y-%m-%d") if period.single_day
            else f"{period.start:%Y-%m-%d} → {period.last_day:%Y-%m-%d}")
    lines = [m.R_H_EVIDENCE.format(span=span), ""]
    if report.generated:
        # The pack is a point in time snapshot: later commits and sessions are not in it,
        # and replacing the latest view needs --overwrite.
        lines += [m.R_GENERATED.format(time=f"{report.generated:%Y-%m-%d %H:%M}",
                                        offset=f"{report.generated:%z}"), ""]
    return lines


def _scope_section(scope: Scope | None) -> list[str]:
    if scope is None:
        return []
    uncollected = m.R_SCOPE_UNCOLLECTED
    git_scope = (", ".join(scope.repos) if scope.git and scope.repos
                 else uncollected if not scope.git else m.R_SCOPE_GIT_EMPTY)
    lines = [m.R_H_SCOPE,
             m.R_SCOPE_GIT_LINE.format(detail=git_scope),
             m.R_SCOPE_SESSIONS_LINE.format(detail=", ".join(scope.sessions)
                                            if scope.sessions else uncollected),
             m.R_SCOPE_DIRTY_LINE.format(detail=m.R_SCOPE_DIRTY_YES if scope.dirty
                                         else uncollected)]
    if scope.only_repos:
        lines.append(m.R_SCOPE_ONLY_REPOS_LINE.format(detail=", ".join(scope.only_repos)))
    return lines + [""]


def _source_counts(diag: SourceDiagnostics) -> str:
    """Only the counters that exist for this kind of source; no cross-source zeros."""
    if isinstance(diag, GitDiagnostics):
        return m.R_DIAG_GIT_SUFFIX.format_map(vars(diag))
    if isinstance(diag, SessionDiagnostics):
        return m.R_DIAG_SESSION_SUFFIX.format_map(vars(diag))
    return ""


def _diagnostics_section(diagnostics: dict[str, SourceDiagnostics]) -> list[str]:
    if not diagnostics:
        return []
    lines = [m.R_H_DIAG, ""]
    for name, diag in diagnostics.items():
        lines.append(m.R_DIAG_LINE.format_map(
            {**vars(diag), "name": name, "status": coverage_status(diag)}) + _source_counts(diag))
        if isinstance(diag, SessionDiagnostics) and diag.issues:
            lines.append(m.R_DIAG_ISSUES_HEAD)
            for issue in diag.issues:
                record_type = (m.R_DIAG_ISSUE_RECORD_TYPE.format_map(vars(issue))
                               if issue.record_type else "")
                lines.append(m.R_DIAG_ISSUE_LINE.format_map(
                    {**vars(issue), "record_type": record_type}))
    git = diagnostics.get("git")
    if git is not None and (git.local_commits or git.commits_without_local_copy):
        lines.append(m.R_DIAG_GIT_EXTRA.format_map(vars(git)))
    return lines + [m.R_DIAG_FOOTER, ""]


def _fetch_section(results: list[FetchResult]) -> list[str]:
    if not results:
        return []
    lines = [m.R_H_FETCH, ""]
    for result in results:
        lines.append(m.R_FETCH_LINE.format(repo_path=result.repo_path, detail=result.detail,
                                           checked_at=result.checked_at))
    return lines + [m.R_FETCH_FOOTER, ""]


def _pr_section(diag: PullRequestDiagnostics | None,
                pull_requests: list[PullRequest]) -> list[str]:
    """First level index: the PRs involved this period and how many own commits each yielded."""
    if diag is None:
        return []
    lines = [m.R_H_PR, "", m.R_PR_SUMMARY.format_map(vars(diag))]
    if diag.detail:
        lines.append(diag.detail)
    if diag.out_of_scope:
        lines.append(m.R_PR_OUT_OF_SCOPE.format(count=diag.out_of_scope))
    if diag.unsupported_repos:
        lines.append(m.R_PR_UNSUPPORTED.format(
            repos=", ".join(sorted(set(diag.unsupported_repos)))))
    lines += [m.R_PR_INVOLVES_NOTE, ""]
    for pr in pull_requests:
        state = _PR_STATE.get(pr.involvement, m.R_PR_STATE_UNKNOWN).format(
            n=len(pr.associated_commits))
        lines.append(m.R_PR_LINE.format_map(
            {**vars(pr), "title": one_line(pr.title, limits.PR_TITLE), "state": state}))
        if pr.associated_commits:
            lines.append(m.R_PR_COMMITS_LINE.format(
                shas=", ".join(c.full_sha for c in pr.associated_commits)))
    return lines + [""]


def _topic_section(commits: list[Commit]) -> list[str]:
    rows = scope_summary(commits)
    if not rows:
        return []
    lines = [m.R_H_TOPIC]
    for name, total, types in rows:
        lines.append(m.R_TOPIC_LINE.format(scope=name, total=total,
                                            types=" / ".join(f"{t} {n}" for t, n in types)))
    return lines + [""]


def _commit_flags(commit: Commit) -> str:
    flags = []
    if commit.files is None:
        flags.append(m.R_COMMIT_FLAG_NO_LOCAL)
    if commit.revert:
        flags.append(m.R_COMMIT_FLAG_REVERT)
    if commit.reverted_by:
        flags.append(m.R_COMMIT_FLAG_REVERTED_BY.format(sha=commit.reverted_by))
    if commit.reverts_sha:
        flags.append(m.R_COMMIT_FLAG_REVERTS.format(sha=commit.reverts_sha))
    if commit.copies:
        flags.append(m.R_COMMIT_FLAG_COPIES.format(count=commit.copies))
    return ("  " + "；".join(flags)) if flags else ""


def _authored_note(commit: Commit) -> str:
    """Flag an author/committer gap over a day: rebase or cherry-pick is not new work."""
    if not commit.authored or commit.authored[:10] == commit.at[:10]:
        return ""
    try:
        gap = abs((datetime.fromisoformat(commit.at) -
                   datetime.fromisoformat(commit.authored)).days)
    except ValueError:
        gap = 0
    return m.R_COMMIT_AUTHORED.format(authored_date=commit.authored[5:10]) if gap > 1 else ""


def _commits_section(commits: list[Commit], extras: int) -> list[str]:
    head = (m.R_H_COMMITS_WITH_EXTRAS.format(count=len(commits), extras=extras) if extras
            else m.R_H_COMMITS_BARE.format(count=len(commits)))
    lines = [head]
    if not commits:
        lines.append(m.R_COMMITS_EMPTY)
    by_repo: dict[str, list[Commit]] = {}
    for commit in commits:
        by_repo.setdefault(commit.repo, []).append(commit)
    for repo, items in by_repo.items():
        lines.append(m.R_COMMIT_REPO_HEADER.format(repo=repo))
        for commit in items:
            size = (m.R_COMMIT_SIZE_UNKNOWN if commit.files is None
                    else m.R_COMMIT_SIZE.format_map(
                        {**vars(commit), "files": len(commit.files)}))
            lines.append(m.R_COMMIT_LINE.format_map(
                {**vars(commit), "date": commit.at[5:10],
                 "authored_note": _authored_note(commit),
                 "flags": _commit_flags(commit), "size": size}))
            source = ", ".join(commit.pr_ids) if commit.pr_ids else m.R_COMMIT_SOURCE_NONE
            lines.append(m.R_COMMIT_EVIDENCE.format_map(
                {**vars(commit), "repo_path": commit.repo_path or repo, "source": source}))
            if commit.files:
                lines.append("  " + ", ".join(commit.files[:limits.FILES_SHOWN]))
    return lines + [""]


def _session_index(sessions: list[Session]) -> list[str]:
    lines = [m.R_H_SESSION_INDEX, m.R_SESSION_INDEX_FOOTER]
    for session in sessions:
        detail = m.R_SESSION_DETAIL_EXPANDED if session.detailed else m.R_SESSION_DETAIL_OMITTED
        hint, basis = session_index_hint(session)
        signal = (m.R_SESSION_SIGNAL_USER if basis == EventKind.USER_REQUEST
                  else m.R_SESSION_SIGNAL_ASSISTANT if basis == EventKind.ASSISTANT_STATEMENT
                  else m.R_SESSION_SIGNAL_SCREEN)
        hint_suffix = m.R_SESSION_INDEX_HINT.format(hint=hint) if hint else ""
        lines.append(m.R_SESSION_INDEX_LINE.format_map(
            {**vars(session), "detail": detail, "signal": signal,
             "project": Path(session.project).name, "hint": hint_suffix}))
        if session.source:
            lines.append(m.R_SESSION_INDEX_SOURCE.format_map(vars(session)))
    return lines + [""]


def _session_events(session: Session) -> list[str]:
    """Excerpt the window's events.

    Requests keep head and tail, assistant statements and tool results keep the last few;
    everything else is looked up in the structured evidence.
    """
    lines = []
    user_cap = limits.USER_HEAD + limits.USER_TAIL
    requests = [e for e in session.events if e.kind == EventKind.USER_REQUEST]
    if len(requests) > user_cap:
        lines.append(m.R_H_EVENTS_USER_OMITTED.format(count=len(requests) - user_cap))
        requests = requests[:limits.USER_HEAD] + requests[-limits.USER_TAIL:]
    for event in requests:
        lines.append(m.R_EVENT_USER.format(excerpt=event_excerpt(event, limits.USER_REQUEST)))
    for event in [e for e in session.events
                  if e.kind == EventKind.ASSISTANT_STATEMENT][-limits.ASSISTANT_TAIL:]:
        lines.append(m.R_EVENT_ASSISTANT.format(excerpt=event_excerpt(event)))
    calls = {e.call_id: e for e in session.events
             if e.kind == EventKind.TOOL_CALL and e.call_id}
    for event in [e for e in session.events
                  if e.kind == EventKind.TOOL_RESULT][-limits.TOOL_RESULT_TAIL:]:
        if event.call_id in calls:
            lines.append(m.R_EVENT_TOOL_CALL.format(excerpt=event_excerpt(calls[event.call_id])))
        lines.append(m.R_EVENT_TOOL_RESULT.format(excerpt=event_excerpt(event)))
    return lines + [m.R_EVENT_TOTAL.format(count=len(session.events))]


def _session_detail(session: Session, single_day: bool) -> list[str]:
    project = Path(session.project).name if session.project not in (".", "/") else "?"
    span = (f"{session.start:%H:%M}–{session.end:%H:%M}"
            if single_day and session.start.date() == session.end.date()
            else f"{session.start:%m-%d %H:%M}–{session.end:%m-%d %H:%M}")
    dup = m.R_SESSION_HEADER_DUP.format(count=session.dup_n) if session.dup_n else ""
    lines = [m.R_SESSION_HEADER.format_map(
        {**vars(session), "project": project, "span": span,
         "short_id": short_id(session.id), "dup": dup})]
    if session.events:
        lines += _session_events(session)
    elif session.users:
        users = session.users
        cap = limits.USER_HEAD + limits.USER_TAIL
        if len(users) > cap:  # Long sessions often pivot late: keep head and tail
            users = (users[:limits.USER_HEAD]
                     + [m.R_SESSION_USER_OMITTED.format(count=len(users) - cap)]
                     + users[-limits.USER_TAIL:])
        lines.append(m.R_SESSION_USER_LINE.format(detail=" / ".join(users)))
    if session.concl and not session.events:
        for text in session.concl[-limits.ASSISTANT_TAIL:]:
            lines.append(m.R_SESSION_ASSISTANT_LINE.format(text=text))
    if session.tools:
        tools = ", ".join(m.R_SESSION_TOOLS_PAIR.format(name=n, count=v)
                          for n, v in session.tools.most_common(limits.TOOLS_SHOWN))
        line = m.R_SESSION_TOOLS_LINE.format(tools=tools)
        rel = _rel(session.files, session.project)
        if rel:
            line += m.R_SESSION_TOOLS_FILES.format(files=", ".join(rel))
        lines.append(line)
    if session.related:
        links = "; ".join(m.R_SESSION_RELATED_PAIR.format(sha=c.sha, subject=c.subject[:60])
                          for c in session.related)
        lines.append(m.R_SESSION_RELATED.format(links=links))
    return lines + [""]


def _sessions_section(sessions: list[Session], truncated: bool, single_day: bool) -> list[str]:
    header = (m.R_H_SESSIONS_TRUNCATED if truncated else m.R_H_SESSIONS_BARE).format(
        count=len(sessions))
    lines = [header]
    if not sessions:
        return lines + [m.R_SESSIONS_EMPTY]
    lines += _session_index(sessions)
    for session in sorted(sessions, key=lambda s: s.start):
        if session.detailed:
            lines += _session_detail(session, single_day)
    return lines


def _dirty_section(dirty: list[DirtyState], stale_days: int) -> list[str]:
    if not dirty:
        return []
    lines = [m.R_H_DIRTY, m.R_DIRTY_HINT.format(stale_days=stale_days)]
    for state in dirty:
        if state.age_days is None:
            age, mark = m.R_DIRTY_AGE_UNKNOWN, ""
        else:
            age = m.R_DIRTY_AGE_DAYS.format(days=state.age_days)
            mark = m.R_DIRTY_STALE_MARK if state.age_days >= stale_days else ""
        lines.append(m.R_DIRTY_LINE.format(repo=state.repo, files=state.files, age=age, mark=mark))
    return lines + [""]


def _writing_hint(period: str) -> list[str]:
    return [m.R_H_WRITING, m.R_WRITING_BODY.format(period=period)]


def render(report: Report) -> str:
    single_day = report.period.single_day
    lines = _header(report)
    lines += _scope_section(report.scope)
    lines += _diagnostics_section(report.diagnostics)
    lines += _fetch_section(report.fetch_results)
    lines += _pr_section(report.pr_diagnostics, report.pull_requests)
    lines += _topic_section(report.commits)
    lines += _commits_section(report.commits, report.extras)
    lines += _sessions_section(report.sessions, report.truncated, single_day)
    lines += _dirty_section(report.dirty, report.stale_days)
    lines += _writing_hint(report.period_word or _period_word(report.period.days))
    # Lets a later run detect evidence that disappeared between collections
    lines += ["", format_counts(len(report.commits), len(report.sessions))]
    return "\n".join(lines)
