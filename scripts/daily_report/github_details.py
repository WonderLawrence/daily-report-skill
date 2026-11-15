"""Details for a selected PR: description, state and per-commit messages.

Discovery (`github.discover_pull_requests`) is one layer up and only yields
references;
this layer turns one selected reference into a full `PullRequestDetails` record.
The
change itself is never prefetched — commit messages are enough for the writer,
diffs
are reviewed on demand by SHA.

`api` and `_period_commit` are looked up through the `github` module at call
time, so
`patch.object(github, "api", ...)` in tests propagates here without each test
having
to know whether a given function lives in github or github_details.
"""

from __future__ import annotations

import subprocess

from . import github
from . import messages as m
from .github import GitHubError, SHA, now, valid_repository
from .models import (
    CommitRef,
    Involvement,
    MessageSource,
    MessageStatus,
    PeriodCommit,
    PullRequest,
    PullRequestDetails,
)


_DETAIL_LIMITATIONS = (
    m.D_LIM_DESCRIPTION,
    m.D_LIM_COMMITTER_DATE,
    m.D_LIM_PLATFORM_COMMITS,
    m.D_LIM_SELF_REPORTED,
    m.D_LIM_DEDUP,
)


# ---------- Commit-message fallback chain ----------
# Each stage returns a finished PeriodCommit on success, or None (with the
# local-side
# error recorded on `base`) so the next stage can try. The outer function
# composes them.


def _from_api_cache(base: PeriodCommit, ref: CommitRef) -> PeriodCommit | None:
    """Preferred: the discover layer already captured the message via PR commits
    API.
    """
    if not isinstance(ref.message, str):
        return None
    base.message_status = MessageStatus.OK
    base.message_source = MessageSource.GITHUB_PR_COMMITS
    return base


def _from_local_git(base: PeriodCommit, ref: CommitRef, timeout: int) -> PeriodCommit | None:
    """Fill in the message from the local repository; record a reason on
    failure.
    """
    if not ref.repo_path:
        base.local_message_error = m.GH_LOCAL_NO_REPO
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                ref.repo_path,
                "show",
                "-s",
                "--format=%B",
                "--no-color",
                ref.full_sha,
                "--",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        base.local_message_error = m.GH_LOCAL_TIMEOUT
        return None
    if result.returncode:
        base.local_message_error = m.GH_LOCAL_UNREADABLE
        return None
    base.message_status = MessageStatus.OK
    base.message = result.stdout.strip()
    base.message_source = MessageSource.LOCAL_GIT
    base.message_retrieved_at = now()
    return base


def _from_github_git_commit(base: PeriodCommit, ref: CommitRef, repository: str, timeout: int) -> PeriodCommit:
    """Last-resort: the git-database endpoint carries the message without any
    patch.
    """
    try:
        # Git database objects contain the message without file content or
        # patches.
        data = github.api(f"repos/{repository}/git/commits/{ref.full_sha}", timeout)
        if not isinstance(data, dict) or data.get("sha") != ref.full_sha or not isinstance(data.get("message"), str):
            raise GitHubError(m.GH_ERR_REMOTE_COMMIT_MISMATCH)
    except GitHubError as exc:
        base.message_error = m.GH_REMOTE_FALLBACK_FAILED.format(local_error=base.local_message_error, exc=exc)
        return base
    base.message_status = MessageStatus.OK
    base.message = data["message"]
    base.message_source = MessageSource.GITHUB_GIT_COMMIT
    base.message_retrieved_at = now()
    return base


def _period_commit(ref: CommitRef, repository: str, timeout: int) -> PeriodCommit:
    """Extend a CommitRef with its full commit message, or carry the gap
    forward.

    The change itself is reviewed on demand by the caller and is never
    prefetched.
    """
    base = PeriodCommit(**vars(ref), message_status=MessageStatus.FAILED)
    if not isinstance(ref.full_sha, str) or not SHA.fullmatch(ref.full_sha):
        base.message_error = m.GH_LOCAL_INVALID_SHA
        return base
    return (
        _from_api_cache(base, ref)
        or _from_local_git(base, ref, timeout)
        or _from_github_git_commit(base, ref, repository, timeout)
    )


# ---------- PR description + commit messages ----------
def selected_details(pr: PullRequest, timeout: int = 30) -> PullRequestDetails:
    if not valid_repository(pr.repository) or not isinstance(pr.number, int) or isinstance(pr.number, bool) or pr.number < 1:
        raise GitHubError(m.GH_ERR_INVALID_PR_ID)
    data = github.api(f"repos/{pr.repository}/pulls/{pr.number}", timeout)
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise GitHubError(m.GH_ERR_PR_DETAIL_FORMAT)
    if data.get("body") is not None and not isinstance(data["body"], str):
        raise GitHubError(m.GH_ERR_PR_DESCRIPTION)
    head = data.get("head") or {}
    if not isinstance(head, dict):
        raise GitHubError(m.GH_ERR_PR_HEAD)
    limitations = list(_DETAIL_LIMITATIONS)
    if not pr.associated_commits:
        limitations.append(m.D_LIM_NO_PERIOD_COMMITS)
    if pr.involvement in (Involvement.UNQUERIED, Involvement.FAILED):
        limitations.append(m.D_LIM_INCOMPLETE_QUERY)
    if pr.commit_list_capped:
        limitations.append(m.D_LIM_COMMIT_LIST_CAPPED)
    return PullRequestDetails(
        id=pr.id,
        url=pr.url,
        retrieved_at=now(),
        discovered_at=pr.retrieved_at,
        title=data["title"],
        description=data.get("body") or "",
        state_now=data.get("state"),
        merged_at=data.get("merged_at"),
        closed_at=data.get("closed_at"),
        updated_at=data.get("updated_at"),
        head_sha_now=head.get("sha"),
        involvement=pr.involvement,
        commit_list_capped=pr.commit_list_capped,
        period_commits=[_period_commit(ref, pr.repository, timeout) for ref in pr.associated_commits],
        limitations=limitations,
    )
