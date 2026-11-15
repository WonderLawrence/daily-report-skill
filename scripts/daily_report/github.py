"""Read only GitHub PR discovery: find this period's PRs, then the user's commits in them.

Two levels: `involves:<login>` searches PRs updated in the window (first level), then each
PR's commits are read and filtered by author and committer date (second level).
`involves:` matches author/assignee/mentions/commenter but not commit authorship, so a PR
opened by someone else that contains the user's commits without any comment from them is
invisible; the diagnostics say so. Descriptions are loaded later, by `github_details`,
once the work items have been selected.

Relies on an already authenticated gh CLI: it never logs in, writes to GitHub or stores
credentials.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import messages as m
from .diagnostics import CoverageStatus, PullRequestDiagnostics
from .gitlog import remote_urls
from .models import Commit, CommitRef, Identity, Involvement, PullRequest
from .timewindow import Period, parse_ts


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40,64}\Z")


class GitHubError(Exception):
    def __init__(self, message: str, stop: bool = False):
        super().__init__(message)
        self.stop = stop


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_repository(value) -> bool:
    return (isinstance(value, str) and bool(REPOSITORY.fullmatch(value))
            and all(part not in (".", "..") for part in value.split("/")))


def github_repository(remote: str) -> str | None:
    """Accept github.com HTTPS/SSH remotes; do not guess enterprise hosts or SSH aliases."""
    if remote.startswith("git@github.com:"):
        path = remote.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return None
        if parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix("/").removesuffix(".git")
    return path if valid_repository(path) else None


def api(endpoint: str, timeout: int, paginate: bool = False, item_key: str | None = None):
    cmd = ["gh", "api", "--hostname", "github.com", "--method", "GET",
           "-H", "Accept: application/vnd.github+json", endpoint]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                timeout=timeout, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise GitHubError(m.GH_ERR_QUERY_FAILED, stop=True) from exc
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", result.stderr)
        code = status.group(1) if status else "unknown"
        raise GitHubError(m.GH_ERR_HTTP_STATUS.format(code=code),
                          stop=code in ("401", "403", "429", "unknown"))
    try:
        data = json.loads(result.stdout)
        if item_key is not None:
            # Search endpoints return one object per page, with the items under item_key
            if not isinstance(data, dict) or not isinstance(data.get(item_key), list):
                raise ValueError("expected search results")
            return data
        if paginate:
            if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
                raise ValueError("expected pages")
            return [item for page in data for item in page]
        return data
    except (ValueError, TypeError) as exc:
        raise GitHubError(m.GH_ERR_UNRECOGNIZED, stop=True) from exc


def repo_index(repos: list[Path], timeout: int) -> dict[str, Path]:
    """owner/repo -> local repository path, used for PR scoping and resolving changes by SHA."""
    index: dict[str, Path] = {}
    for repo in repos:
        try:
            urls = remote_urls(repo, timeout)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            continue
        for url in urls:
            name = github_repository(url)
            if name:
                index.setdefault(name, repo)
    return index


SEARCH_LIMIT = 100
SEARCH_MAX_RESULTS = 1000


def viewer_login(timeout: int) -> str | None:
    """Login of the authenticated account, used to spot the user's commits inside a PR.

    Without it, only the configured emails are matched.
    """
    try:
        data = api("user", timeout)
    except GitHubError:
        return None
    login = data.get("login") if isinstance(data, dict) else None
    return login if isinstance(login, str) and login else None


def repository_from_url(url) -> str | None:
    """A search result's repository_url looks like https://api.github.com/repos/owner/name."""
    if not isinstance(url, str):
        return None
    name = url.rsplit("/repos/", 1)[-1] if "/repos/" in url else ""
    return name if valid_repository(name) else None


def search_pull_requests(start: datetime, timeout: int,
                         login: str | None = None, repository: str | None = None,
                         page: int = 1) -> tuple[list[dict], int, bool]:
    """Read one search page, including the platform's coverage signals.

    Substitute the real login whenever possible: `@me` is sugar understood by the web UI and
    the gh CLI, and the REST search may not expand it server side. If it does not, the query
    silently returns nothing, which looks exactly like "no work this period".
    """
    query = f"type:pr involves:{login or '@me'} updated:>={start.isoformat()}"
    if repository is not None:
        query += f" repo:{repository}"
    endpoint = (f"search/issues?q={quote(query)}&advanced_search=true"
                f"&sort=updated&order=desc&per_page={SEARCH_LIMIT}&page={page}")
    data = api(endpoint, timeout, item_key="items")
    total = data.get("total_count")
    items = [item for item in data["items"] if isinstance(item, dict)]
    return items, total if isinstance(total, int) else len(items), bool(data.get("incomplete_results"))


def _note(diag: PullRequestDiagnostics, message: str) -> None:
    diag.detail = "; ".join(filter(None, (diag.detail, message)))


def _search_candidates(period: Period, login: str | None, repositories: list[str | None],
                       timeout: int, max_queries: int, diag: PullRequestDiagnostics
                       ) -> tuple[list[dict], bool]:
    """Scope before pagination; keep earlier results if a later search fails."""
    items, seen = [], set()
    blocked, succeeded = False, False
    for index, repository in enumerate(repositories):
        if blocked or diag.queries >= max_queries:
            diag.searches_skipped = len(repositories) - index
            _note(diag, m.GH_DETAIL_SEARCHES_SKIPPED.format(count=diag.searches_skipped))
            break
        label = repository or m.GH_SEARCH_LABEL_ALL
        page, received = 1, 0
        while True:
            diag.queries += 1
            try:
                batch, total, incomplete = search_pull_requests(
                    period.start, timeout, login, repository, page)
            except GitHubError as exc:
                diag.failures += 1
                diag.search_truncated = True
                _note(diag, m.GH_DETAIL_SEARCH_PAGE_ERROR.format(label=label, page=page, error=exc))
                blocked = exc.stop
                break
            succeeded = True
            received += len(batch)
            for item in batch:
                key = (item.get("repository_url"), item.get("number"))
                if not isinstance(key[0], str) or type(key[1]) is not int:
                    # Leave malformed records to _screen, which records the coverage gap.
                    items.append(item)
                    continue
                if key not in seen:
                    seen.add(key)
                    items.append(item)
            if incomplete:
                diag.search_incomplete = True
                _note(diag, m.GH_DETAIL_SEARCH_INCOMPLETE.format(label=label, page=page))
            if received >= total:
                break
            if (len(batch) < SEARCH_LIMIT or received >= SEARCH_MAX_RESULTS
                    or diag.queries >= max_queries):
                diag.search_truncated = True
                reason = (m.GH_REASON_SHORT_PAGE if len(batch) < SEARCH_LIMIT else
                          m.GH_REASON_HIT_LIMIT if received >= SEARCH_MAX_RESULTS
                          else m.GH_REASON_QUERY_BUDGET)
                _note(diag, m.GH_DETAIL_SEARCH_TRUNCATED.format(
                    label=label, total=total, received=received, reason=reason))
                break
            page += 1
    if diag.failures and not succeeded:
        diag.status = CoverageStatus.UNAVAILABLE
    return items, blocked


def pull_request_commits(repository: str, number: int, timeout: int) -> list[dict]:
    """Second level: the PR's commit list (the platform returns at most 250)."""
    data = api(f"repos/{repository}/pulls/{number}/commits?per_page=100", timeout, paginate=True)
    return [item for item in data if isinstance(item, dict)]


def _is_mine(raw: dict, identity: Identity) -> bool:
    """Keep explicit author restrictions; use login/email fallback only in automatic mode."""
    if identity.any_author:
        return True
    if not identity.author_explicit and identity.login and isinstance(raw.get("author"), dict) \
            and raw["author"].get("login") == identity.login:
        return True
    author = (raw.get("commit") or {}).get("author") or {}
    return identity.matches_git_author(
        f"{author.get('name', '')} <{author.get('email', '')}>")


def _build_commit(raw: dict, repository: str, repo_index: dict[str, Path],
                  tz: timezone) -> Commit | None:
    """Turn a platform commit into an evidence record.

    Change details stay empty; gitlog fills them in later by SHA.
    """
    sha = raw.get("sha")
    commit = raw.get("commit") or {}
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    landed = parse_ts(committer.get("date"), tz)
    written = parse_ts(author.get("date"), tz) or landed
    message = commit.get("message")
    if not isinstance(sha, str) or not SHA.fullmatch(sha) or landed is None \
            or not isinstance(message, str):
        return None
    local = repo_index.get(repository)
    subject = message.splitlines()[0] if message.strip() else ""
    return Commit(
        repo=local.name if local else repository.split("/")[-1],
        repo_path=str(local) if local else "", full_sha=sha, sha=sha[:7],
        at=landed.isoformat(), authored=written.isoformat(),
        author=f"{author.get('name', '')} <{author.get('email', '')}>".strip(),
        subject=subject, revert=subject.startswith('Revert "'),
        message=message, message_retrieved_at=now())


def _screen(item: dict, repo_index: dict[str, Path], scoped: bool,
            diag: PullRequestDiagnostics) -> tuple[str, int] | None:
    """Search item -> (owner/repo, number); malformed or out of scope items drop out here."""
    repository = repository_from_url(item.get("repository_url"))
    number = item.get("number")
    if not repository or type(number) is not int or number < 1 \
            or not isinstance(item.get("title"), str):
        diag.failures += 1
        return None
    if repository not in repo_index:
        if scoped:
            diag.out_of_scope += 1
            return None
        diag.unsupported_repos.append(repository)
    return repository, number


def _pr_record(item: dict, repository: str, number: int) -> PullRequest:
    """Metadata for a PR of this period; the description is left to pr_details."""
    return PullRequest(
        id=f"{repository}#{number}", repository=repository, number=number,
        url=item.get("html_url") or f"https://github.com/{repository}/pull/{number}",
        title=item["title"], state_at_discovery=item.get("state"),
        author_login=(item.get("user") or {}).get("login"),
        updated_at=item.get("updated_at"), retrieved_at=now())


def _own_commits(raw_commits: list[dict], repository: str, identity: Identity,
                 period: Period, repo_index: dict[str, Path],
                 diag: PullRequestDiagnostics) -> list[Commit]:
    """Second level: within a PR's commits, the user's, with committer date in the window."""
    kept = []
    for raw in raw_commits:
        if not _is_mine(raw, identity):
            continue
        commit = _build_commit(raw, repository, repo_index, period.tz)
        if commit is None:
            diag.failures += 1
            continue
        if period.contains(parse_ts(commit.at, period.tz)):
            kept.append(commit)
    return kept


def discover_pull_requests(period: Period, identity: Identity,
                           repo_index: dict[str, Path] | None = None,
                           scoped: bool = False, enabled: bool = True, timeout: int = 30,
                           max_queries: int = 200
                           ) -> tuple[list[PullRequest], list[Commit], PullRequestDiagnostics]:
    """Two level traversal: PRs involving the user -> their in window commits inside them."""
    repo_index = repo_index or {}
    diag = PullRequestDiagnostics(checked_at=now())
    if not enabled:
        diag.status = CoverageStatus.DISABLED
        return [], [], diag
    if scoped and not repo_index:
        diag.status = CoverageStatus.UNAVAILABLE
        diag.detail = m.GH_DETAIL_REPO_NO_REMOTE
        return [], [], diag
    if not shutil.which("gh"):
        diag.status = CoverageStatus.UNAVAILABLE
        diag.detail = m.GH_DETAIL_GH_MISSING
        return [], [], diag

    # Resolve the login on a copy: discovery must not mutate its caller's identity
    identity = replace(identity, login=viewer_login(timeout))
    if not identity.login:
        diag.detail = (m.GH_DETAIL_NO_LOGIN_EXPLICIT if identity.author_explicit
                       else m.GH_DETAIL_NO_LOGIN_EMAILS)
    repositories = sorted(repo_index) if scoped else [None]
    items, blocked = _search_candidates(period, identity.login, repositories,
                                        timeout, max_queries, diag)
    diag.prs_found = len(items)

    records: list[PullRequest] = []
    commits: dict[str, Commit] = {}
    for item in items:
        screened = _screen(item, repo_index, scoped, diag)
        if screened is None:
            continue
        repository, number = screened
        record = _pr_record(item, repository, number)
        records.append(record)
        if blocked or diag.queries >= max_queries:
            diag.unqueried += 1
            record.involvement = Involvement.UNQUERIED
            continue
        diag.queries += 1
        try:
            raw_commits = pull_request_commits(repository, number, timeout)
        except GitHubError as exc:
            diag.failures += 1
            _note(diag, str(exc))
            blocked = blocked or exc.stop
            record.involvement = Involvement.FAILED
            continue
        if len(raw_commits) >= 250:
            record.commit_list_capped = True
            diag.commit_lists_capped += 1
            _note(diag, m.GH_DETAIL_COMMIT_LIST_CAPPED.format(pr_id=record.id))
        for commit in _own_commits(raw_commits, repository, identity, period,
                                   repo_index, diag):
            kept = commits.setdefault(commit.full_sha, commit)
            if record.id not in kept.pr_ids:
                kept.pr_ids.append(record.id)
            if not any(c.full_sha == kept.full_sha for c in record.associated_commits):
                record.associated_commits.append(_commit_reference(kept))
        record.involvement = (Involvement.COMMITS if record.associated_commits
                              else Involvement.NO_PERIOD_COMMITS)

    diag.prs_with_commits = sum(1 for r in records if r.involvement == Involvement.COMMITS)
    diag.prs_without_commits = sum(r.involvement == Involvement.NO_PERIOD_COMMITS for r in records)
    if diag.status != CoverageStatus.UNAVAILABLE and (
            diag.failures or diag.unqueried or diag.search_truncated or diag.search_incomplete
            or diag.searches_skipped or diag.commit_lists_capped):
        diag.status = CoverageStatus.PARTIAL
    return (sorted(records, key=lambda record: record.id),
            sorted(commits.values(), key=lambda commit: commit.at), diag)


def _commit_reference(commit: Commit) -> CommitRef:
    """Keep the commit locator and API message; change content is never copied."""
    return CommitRef(full_sha=commit.full_sha, repo_path=commit.repo_path, at=commit.at,
                     authored=commit.authored, author=commit.author, subject=commit.subject,
                     message=commit.message, message_retrieved_at=commit.message_retrieved_at)
