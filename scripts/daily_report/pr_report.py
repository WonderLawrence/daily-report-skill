"""Details for selected PRs: only PRs already present in the collection snapshot are read."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from .evidence import json_evidence, redact_text, write_evidence
from .github import GitHubError, now, selected_details
from .models import (CommitRef, DetailsReport, FetchFailure, PullRequest,
                     PullRequestDetails, Snapshot)
from .text import one_line

_T = TypeVar("_T")


class UnknownPullRequest(Exception):
    """The selected PR is not in the snapshot: refuse to query rather than guess a number."""


def _aware(value: str) -> datetime:
    """Parse a snapshot ISO timestamp; reject naive values so the comparison below can't blow
    up with "offset-naive vs offset-aware", which would surface as a generic read-failure."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("缺少时区信息")
    return parsed


def _project(cls: type[_T], raw: dict, **overrides) -> _T:
    """Rebuild one record from serialized data, with one policy for every record read back.

    Keys this version does not know are dropped, because a newer writer may have added
    fields; a missing *required* field still raises TypeError naming that field, instead of
    letting a `raw["foo"]` KeyError surface three functions later.
    """
    known = {f.name for f in dataclasses.fields(cls)} - set(overrides)
    return cls(**{key: raw[key] for key in known if key in raw}, **overrides)


def _hydrate(raw: dict) -> PullRequest:
    """Rebuild one PullRequest, including its commit references, from evidence.json."""
    refs = [_project(CommitRef, commit) for commit in raw.get("associated_commits", [])]
    return _project(PullRequest, raw, associated_commits=refs)


def load_selection(evidence_path: Path,
                   keys: list[str]) -> tuple[Snapshot, dict[str, PullRequest]]:
    """Pick selected PRs, allowing zero period commits (e.g. merge-only or review work).

    Validate any commit references that exist; an empty list is not an invalid snapshot.
    """
    raw = json.loads(evidence_path.read_text())
    try:
        start, end = _aware(raw["start"]), _aware(raw["end_exclusive"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"快照必须包含有效的带时区起止时间：{exc}") from exc
    if start >= end:
        raise ValueError("快照起止时间不合法：start 必须早于 end_exclusive")
    try:
        known = {key: _hydrate(pr) for pr in raw["pull_requests"]
                 for key in (pr["id"], pr["url"])}
    except (TypeError, KeyError) as exc:
        raise ValueError(f"快照 PR 结构不完整：{exc}") from exc
    snapshot = Snapshot(evidence_path, raw["start"], raw["end_exclusive"])
    chosen: dict[str, PullRequest] = {}
    for key in keys:
        if key not in known:
            raise UnknownPullRequest(f"所选 PR 不在该快照中：{key}")
        pr = known[key]
        for commit in pr.associated_commits:
            try:
                at = _aware(commit.at)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"PR 关联提交时间不可用：{exc}") from exc
            if not start <= at < end:
                raise ValueError("PR 关联提交超出快照时间范围")
        chosen[pr.id] = pr
    return snapshot, chosen


def fetch_details(chosen: dict[str, PullRequest], timeout: int
                  ) -> tuple[list[PullRequestDetails], list[FetchFailure]]:
    details, failures = [], []
    for pr in chosen.values():
        try:
            details.append(selected_details(pr, timeout))
        except GitHubError as exc:
            failures.append(FetchFailure(id=pr.id, error=str(exc)))
    return details, failures


def render_markdown(report: DetailsReport) -> str:
    details, failures = report.details, report.failures
    lines = ["# 已选 PR 的分析证据", "",
             f"原始范围：{report.snapshot.start} → {report.snapshot.end_exclusive}",
             "Description 为读取时版本，提交按原快照的本人和时间范围筛选。"
             "完整正文与 commit message 见本次 JSON；改动本身不在本包内，按需自行 review。", ""]
    for pr in details:
        lines += [f"## [{pr.id}]({pr.url}) {one_line(pr.title, 180)}",
                  f"获取时间：{pr.retrieved_at}；读取时状态：{pr.state_now}；"
                  f"合并时间：{pr.merged_at}", ""]
        for commit in pr.period_commits:
            lines.append(f"- `{commit.full_sha}` · {commit.at} · "
                         f"{one_line(commit.subject, 180)}")
            if commit.message:
                lines.append("  message：" + one_line(commit.message, 600))
            if commit.message_error:
                lines.append("  message 缺口：" + commit.message_error)
            lines.append(f"  需要确认改动时自行 review：`git -C {commit.repo_path} "
                         f"show {commit.full_sha}`")
        lines += ["", *pr.limitations, ""]
    for failure in failures:
        lines.append(f"- 缺口 {failure.id}：{failure.error}")
    return "\n".join(lines)


def write_report(out: Path, report: DetailsReport, force: bool) -> str | None:
    """Render, redact and store the selected PR evidence next to its structured payload."""
    payload = json_evidence(report.as_evidence(now()), True)
    text = redact_text(render_markdown(report))
    return write_evidence(out, text, force, payload, redact_previous=True)
