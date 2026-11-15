"""Details for selected PRs: only PRs already present in the collection snapshot
are read.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from . import limits
from . import messages as m
from .evidence import (
    WrittenEvidence,
    json_evidence,
    redact_text,
    write_evidence,
)
from .evidence_reader import load_input
from .github import GitHubError, now
from .github_details import selected_details
from .models import (
    CommitRef,
    DetailsReport,
    FetchFailure,
    PullRequest,
    PullRequestDetails,
    Snapshot,
)
from .text import one_line

_T = TypeVar("_T")


class UnknownPullRequest(Exception):
    """The selected PR is not in the snapshot: refuse to query rather than guess
    a number.
    """


def _aware(value: str) -> datetime:
    """Parse a snapshot ISO timestamp; reject naive values so the comparison
    below can't blow
    up with "offset-naive vs offset-aware", which would surface as a generic
    read-failure.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(m.PR_ERR_TZ_MISSING)
    return parsed


def _project(cls: type[_T], raw: dict, **overrides) -> _T:
    """Rebuild one record from serialized data, with one policy for every record
    read back.

    Keys this version does not know are dropped, because a newer writer may have
    added
    fields; a missing *required* field still raises TypeError naming that field,
    instead of
    letting a `raw["foo"]` KeyError surface three functions later.
    """
    known = {f.name for f in dataclasses.fields(cls)} - set(overrides)
    return cls(**{key: raw[key] for key in known if key in raw}, **overrides)


def _hydrate(raw: dict) -> PullRequest:
    """Rebuild one PullRequest, including its commit references, from
    evidence.json.
    """
    refs = [_project(CommitRef, commit) for commit in raw.get("associated_commits", [])]
    return _project(PullRequest, raw, associated_commits=refs)


def load_selection(evidence_path: Path, keys: list[str]) -> tuple[Snapshot, dict[str, PullRequest]]:
    """Pick selected PRs, allowing zero period commits (e.g. merge-only or
    review work).

    Validate any commit references that exist; an empty list is not an invalid
    snapshot.
    """
    evidence_path, raw = load_input(evidence_path)
    try:
        start, end = _aware(raw["start"]), _aware(raw["end_exclusive"])
    except (TypeError, ValueError) as exc:
        raise ValueError(m.PR_ERR_SNAPSHOT_BOUNDS.format(detail=exc)) from exc
    if start >= end:
        raise ValueError(m.PR_ERR_SNAPSHOT_RANGE)
    try:
        known = {key: _hydrate(pr) for pr in raw["pull_requests"] for key in (pr["id"], pr["url"])}
    except (TypeError, KeyError) as exc:
        raise ValueError(m.PR_ERR_SNAPSHOT_SHAPE.format(detail=exc)) from exc
    snapshot = Snapshot(evidence_path, raw["start"], raw["end_exclusive"])
    chosen: dict[str, PullRequest] = {}
    for key in keys:
        if key not in known:
            raise UnknownPullRequest(m.PR_ERR_UNKNOWN.format(key=key))
        pr = known[key]
        for commit in pr.associated_commits:
            try:
                at = _aware(commit.at)
            except (TypeError, ValueError) as exc:
                raise ValueError(m.PR_ERR_COMMIT_TIME.format(detail=exc)) from exc
            if not start <= at < end:
                raise ValueError(m.PR_ERR_COMMIT_OUT_OF_RANGE)
        chosen[pr.id] = pr
    return snapshot, chosen


def fetch_details(chosen: dict[str, PullRequest], timeout: int) -> tuple[list[PullRequestDetails], list[FetchFailure]]:
    details, failures = [], []
    for pr in chosen.values():
        try:
            details.append(selected_details(pr, timeout))
        except GitHubError as exc:
            failures.append(FetchFailure(id=pr.id, error=str(exc)))
    return details, failures


def render_markdown(report: DetailsReport) -> str:
    details, failures = report.details, report.failures
    summary = report.summary
    lines = [
        m.D_H_EVIDENCE,
        "",
        m.D_SUMMARY_LINE.format_map({**vars(summary), "status": report.status}),
        m.D_COMPLETENESS_NOTE,
        m.D_RANGE_LINE.format(start=report.snapshot.start, end=report.snapshot.end_exclusive),
        m.D_RANGE_FOOTER,
        "",
    ]
    for pr in details:
        lines += [
            m.D_PR_HEADER.format_map({**vars(pr), "title": one_line(pr.title, limits.PR_TITLE)}),
            m.D_PR_META.format_map(vars(pr)),
            "",
        ]
        for commit in pr.period_commits:
            lines.append(
                m.D_COMMIT_LINE.format_map(
                    {
                        **vars(commit),
                        "subject": one_line(commit.subject, limits.PR_TITLE),
                    }
                )
            )
            if commit.message:
                lines.append(m.D_COMMIT_MESSAGE.format(excerpt=one_line(commit.message, limits.COMMIT_MESSAGE)))
            if commit.message_source:
                lines.append(m.D_COMMIT_MESSAGE_SOURCE.format_map(vars(commit)))
            if commit.local_message_error:
                lines.append(m.D_COMMIT_LOCAL_ERROR.format_map({"error": commit.local_message_error}))
            if commit.message_error:
                lines.append(m.D_COMMIT_MESSAGE_GAP.format_map({"error": commit.message_error}))
            lines.append(m.D_COMMIT_REVIEW_HINT.format_map(vars(commit)))
        lines += ["", *pr.limitations, ""]
    for failure in failures:
        lines.append(m.D_FAILURE_LINE.format_map(vars(failure)))
    return "\n".join(lines)


def write_report(out: Path, report: DetailsReport, overwrite: bool) -> WrittenEvidence:
    """Render, redact and store the selected PR evidence next to its structured
    payload.
    """
    payload = json_evidence(report.as_evidence(now()), True)
    text = redact_text(render_markdown(report))
    return write_evidence(out, text, overwrite, payload, redact_previous=True)
