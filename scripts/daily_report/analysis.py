"""Aggregation: clues for grouping work by topic, never a judgement of its
value.

Everything here is a hint (duplicate copies, revert pairing, file overlap,
summary budget)
that still has to be confirmed against the actual task.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import limits
from .gitlog import git_patch_id
from .models import Commit, EventKind, Session
from .text import substantive_text

COMMIT_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<rest>.*)$")


def _patch_groups(items: list[Commit]) -> list[list[Commit]]:
    """Group same title, same files commits by patch id; keep them apart without
    one.
    """
    if len(items) <= 1:
        return [items]
    groups: dict[str, list[Commit]] = {}
    for commit in items:
        # Without a local patch id (unresolved SHA and the like), treat commits
        # as distinct:
        # equal titles and paths are not evidence that two commits are copies.
        patch_id = git_patch_id(Path(commit.repo_path), commit.full_sha) if commit.repo_path else None
        groups.setdefault(patch_id or f"unique:{commit.full_sha}", []).append(commit)
    return list(groups.values())


def collapse_duplicates(commits: list[Commit]) -> tuple[list[Commit], int]:
    """Collapse rebase/cherry-pick copies only when the patch itself matches."""
    by_shape: dict[tuple, list[Commit]] = {}
    for commit in commits:
        shape = tuple(sorted(commit.files)) if commit.files is not None else None
        by_shape.setdefault((commit.repo, commit.subject, shape), []).append(commit)

    shown: list[Commit] = []
    extras = 0
    for items in by_shape.values():
        for same_patch in _patch_groups(items):
            ordered = sorted(same_patch, key=lambda c: c.at)
            # Unknown change size sorts as 0 without rewriting its None meaning
            best = max(ordered, key=lambda c: (c.added or 0) + (c.deleted or 0))
            reverted_by = next(
                (c.reverted_by for c in ordered if c.reverted_by),
                best.reverted_by,
            )
            keep = replace(best, copies=len(ordered) - 1, reverted_by=reverted_by)
            extras += keep.copies
            shown.append(keep)
    shown.sort(key=lambda commit: commit.at)
    return shown, extras


def scope_summary(
    commits: list[Commit],
) -> list[tuple[str, int, list[tuple[str, int]]]]:
    """Cluster by conventional commit scope to give topic grouping a starting
    point.
    """
    per_scope: dict[str, Counter] = {}
    for commit in commits:
        m = COMMIT_RE.match(commit.subject)
        typ = m.group("type").lower() if m else "other"
        scope = (m.group("scope") if m and m.group("scope") else "(无 scope)").strip()
        per_scope.setdefault(scope, Counter())[typ] += 1
    rows = [(s, sum(cnt.values()), cnt.most_common(limits.TOPIC_TYPES)) for s, cnt in per_scope.items()]
    rows.sort(key=lambda r: -r[1])
    return rows[: limits.TOPIC_SCOPES]


def _revert_candidates(commit: Commit, commits: list[Commit], by_subject: dict[tuple, list[Commit]]) -> list[Commit]:
    target = commit.revert_target
    if target:
        found = [
            item
            for item in commits
            if item.repo == commit.repo and item.full_sha.startswith(target) and item.full_sha != commit.full_sha
        ]
        return found if len(found) == 1 else []
    if not commit.subject.startswith('Revert "'):
        return []
    inner = commit.subject[len('Revert "') :].rstrip('"').strip()
    found = [item for item in by_subject.get((commit.repo, inner), []) if item.full_sha != commit.full_sha]
    # Without a target SHA, pair only when the title identifies one commit.
    return found if len(found) == 1 else []


def pair_reverts(commits: list[Commit]) -> dict[str, str]:
    """Pair by the target SHA in Git's revert body; older records fall back to a
    unique title.
    """
    by_subject: dict[tuple, list[Commit]] = {}
    for commit in commits:
        by_subject.setdefault((commit.repo, commit.subject), []).append(commit)
    pairs: dict[str, str] = {}
    for commit in commits:
        if not commit.revert:
            continue
        candidates = _revert_candidates(commit, commits, by_subject)
        if candidates:
            candidate = candidates[0]
            pairs[candidate.full_sha] = commit.full_sha
            candidate.reverted_by = commit.sha
            commit.reverts_sha = candidate.sha
        elif commit.revert_target:
            commit.reverts_sha = commit.revert_target[:7]
    return pairs


def link_sessions(sessions: list[Session], commits: list[Commit]) -> None:
    """Weak session to commit link: same repository plus a matching relative
    file path.

    A hint, not causation; rendering labels it "possibly related".
    """
    by_repo_file: dict[tuple, list[Commit]] = {}
    for commit in commits:
        for name in commit.files or []:
            by_repo_file.setdefault((commit.repo, name), []).append(commit)
    for session in sessions:
        root = Path(session.project)
        hits: list[Commit] = []
        seen: set[str] = set()
        for path in session.files:
            try:
                rel = str(Path(path).relative_to(root))
            except ValueError:
                continue
            for commit in by_repo_file.get((root.name, rel), []):
                if commit.full_sha not in seen:
                    seen.add(commit.full_sha)
                    hits.append(commit)
        session.related = hits[: limits.RELATED_COMMITS]


def mark_near_duplicates(sessions: list[Session]) -> None:
    """Flag identical substantive requests, never shared prefixes or runtime
    wrappers.

    Use full events because `users` contains truncated summaries. Matching
    requests
    suggest a relation only; sessions are never merged or discarded here.
    """
    groups: dict[tuple, list[Session]] = {}
    for session in sessions:
        session.dup_n = 0
        opening = next(
            (
                " ".join(event.text.split())
                for event in session.events
                if event.kind == EventKind.USER_REQUEST and substantive_text(event.text)
            ),
            None,
        )
        if opening is None:
            continue
        groups.setdefault((session.agent, session.project, opening), []).append(session)
    for group in groups.values():
        if len(group) > 1:
            for session in group:
                session.dup_n = len(group)


def select_session_details(sessions: list[Session], limit: int) -> None:
    """Round-robin dates, then projects; tool count does not determine
    importance.
    """
    dates: dict[date, dict[str, list[Session]]] = {}
    for session in sessions:
        session.detailed = False
        projects = dates.setdefault(session.start.date(), {})
        projects.setdefault(session.project, []).append(session)
    queues = []
    for projects in dates.values():
        queue = []
        groups = list(projects.values())
        while any(groups):
            for group in groups:
                if group:
                    queue.append(group.pop(0))
        queues.append(queue)
    while limit and any(queues):
        for queue in queues:
            if queue and limit:
                queue.pop(0).detailed = True
                limit -= 1
