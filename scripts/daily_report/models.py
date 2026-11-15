"""In-memory evidence records.

These dataclasses are the single description of what a commit, session or event holds.
`evidence.json_evidence` serializes them field by field, so the field order here is the
key order in evidence.json. `at` and `authored` stay ISO strings as produced upstream
rather than becoming datetimes, so no timezone guessing creeps into collection.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .diagnostics import PullRequestDiagnostics, SourceDiagnostics
from .timewindow import Period


@dataclass
class Identity:
    """Explicit author selection wins; otherwise use GitHub login with email fallback."""

    author_re: str | None = None    # Built from author_emails, or given by --author
    any_author: bool = False        # Do not filter by author at all
    name: str = ""                  # git user.name, used to warn about unconfigured emails
    email: str = ""
    login: str | None = None        # GitHub account read from gh at run time
    author_explicit: bool = False   # --author must not be broadened by the GitHub login

    def matches_git_author(self, who: str) -> bool:
        """`who` is Git's "Name <email>" form."""
        if self.any_author:
            return True
        return bool(self.author_re and re.search(self.author_re, who, re.I))

    def same_name_other_email(self, who: str) -> bool:
        """Same name but an unconfigured email: the real signal of a missing identity,
        as opposed to a teammate's commit.
        """
        return bool(self.name) and self.name.lower() in who.lower()


@dataclass
class Event:
    """One retained record from a session file.

    User requests, assistant statements and tool results are stored apart; none of them
    stands in for another.
    """

    kind: str                       # user_request / assistant_statement / tool_call / tool_result
    text: str
    call_id: str | int | None = None
    at: datetime | None = None      # Event time, inside the collection window
    source: str = ""                # Path of the original file
    line: int = 0                   # Line in that file (DSH counts the decompressed JSONL)


@dataclass
class Commit:
    """One of the user's commits that landed in the window, attributed by committer date.

    Produced by the two level PR traversal: the platform supplies identity and timing, the
    local repository fills in changed files and line counts by SHA.
    """

    repo: str
    repo_path: str
    full_sha: str
    sha: str
    at: str
    authored: str
    author: str
    subject: str
    # None = no local copy of this commit, so the change is unknown;
    # [] / 0 means "genuinely no change"
    files: list[str] | None = None
    added: int | None = None
    deleted: int | None = None
    revert: bool = False
    revert_target: str | None = None    # Target SHA parsed from Git's revert body
    reverts_sha: str | None = None      # What this commit reverted
    reverted_by: str | None = None      # What reverted this commit
    copies: int = 0                     # rebase/cherry-pick copies with the same patch id
    pr_ids: list[str] = field(default_factory=list)   # Associated PRs; there may be several

    @property
    def local_available(self) -> bool:
        """Change details exist only once the commit has been resolved locally."""
        return self.files is not None


@dataclass
class Session:
    """One agent session.

    The summary fields are lossy; the full record lives in `events`, where source/line
    locate the original lines.
    """

    agent: str                      # claude-code / codex / dsh
    project: str                    # Parsed cwd, or a guess from the source directory
    id: str
    start: datetime
    end: datetime
    users: list[str] = field(default_factory=list)
    concl: list[str] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    files: set[str] = field(default_factory=set)
    steps: int = 0
    events: list[Event] = field(default_factory=list)
    source: str = ""
    detailed: bool = True           # Whether to expand a detailed summary; the index is complete either way
    related: list[Commit] = field(default_factory=list)   # File path overlap; a hint, not causation
    dup_n: int = 0                  # Number of sessions sharing this opening prompt


@dataclass
class CommitRef:
    """A commit referenced from a PR; the change itself is not copied.

    Serialized field-by-field into `associated_commits` in evidence.json; the key order
    matches the field order below.
    """

    full_sha: str
    repo_path: str
    at: str
    authored: str
    author: str
    subject: str


class Involvement:
    """How a PR relates to the user this period; replaces the four magic strings.

    The values are the on-disk form in evidence.json; readers compare to these constants
    rather than to the literal strings, so a renamed value cannot silently drift.
    """

    COMMITS = "commits"                      # Own commits land in this PR, in window
    NO_PERIOD_COMMITS = "no_period_commits"  # Discovered, but no own in-window commits
    UNQUERIED = "unqueried"                  # Query budget exhausted or blocked
    FAILED = "failed"                        # Commit fetch failed


@dataclass
class PullRequest:
    """One PR discovered this period.

    The description is not here; pr_details.py reads it on demand after selection.
    Field order is the on-disk key order for evidence.json.
    """

    id: str
    repository: str
    number: int
    url: str
    title: str
    state_at_discovery: str | None
    author_login: str | None
    updated_at: str | None
    retrieved_at: str
    involvement: str = Involvement.NO_PERIOD_COMMITS
    associated_commits: list[CommitRef] = field(default_factory=list)


class MessageStatus:
    """Whether a PR commit's message could be read from the local repository."""

    OK = "ok"
    FAILED = "failed"


@dataclass
class PeriodCommit(CommitRef):
    """A CommitRef extended with the commit message read via `git show`.

    Inheriting keeps the reference fields in one place and preserves their on-disk order:
    the parent's fields come first, the message fields after.
    """

    message_status: str = MessageStatus.FAILED
    message: str = ""
    message_error: str = ""


@dataclass
class PullRequestDetails:
    """A selected PR with its description and commit messages; produced after selection."""

    id: str
    url: str
    retrieved_at: str
    discovered_at: str
    title: str
    description: str
    state_now: str | None
    merged_at: str | None
    closed_at: str | None
    updated_at: str | None
    head_sha_now: str | None
    period_commits: list[PeriodCommit] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    """A collection snapshot read back from evidence.json.

    `start` and `end_exclusive` keep the file's exact strings: they are copied verbatim into
    the pr-details payload, and load_selection has already checked they carry a timezone.
    """

    path: Path
    start: str
    end_exclusive: str


@dataclass
class FetchFailure:
    """One PR that failed to fetch details; the id locates it back in the snapshot."""

    id: str
    error: str


@dataclass
class DetailsReport:
    """The result of one pr-details run, mirroring Report on the collection side."""

    snapshot: Snapshot
    selected: list[str] = field(default_factory=list)
    details: list[PullRequestDetails] = field(default_factory=list)
    failures: list[FetchFailure] = field(default_factory=list)

    def as_evidence(self, generated_at: str) -> dict:
        """Shape of the pr-details JSON; this key order is the order in the file."""
        return {"schema_version": 1, "generated_at": generated_at,
                "source_evidence": str(self.snapshot.path), "start": self.snapshot.start,
                "end_exclusive": self.snapshot.end_exclusive, "selected_prs": self.selected,
                "pull_requests": self.details, "failures": self.failures}


class FetchStatus:
    """Outcome of one repository fetch; the values are the on-disk form."""

    OK = "ok"
    SKIPPED = "skipped"            # Disabled by configuration or --no-fetch
    NO_REMOTE = "no_remote"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class FetchResult:
    """Per repository fetch result; success only means it ran against the current remotes."""

    repo: str
    repo_path: str
    remotes: list[str] = field(default_factory=list)
    status: str = FetchStatus.SKIPPED
    detail: str = ""
    checked_at: str = ""


@dataclass
class DirtyState:
    """Run time worktree snapshot: tracked files only, with no guess at how old the change is."""

    repo: str
    files: int
    age_days: int | None            # Days since the last commit; None when unreadable


@dataclass
class Scope:
    """What this run actually looked at; rendered as the "采集范围" section.

    Field order is the on-disk key order in evidence.json. A report without a scope means
    the run never recorded one, which is why Report.scope stays nullable.
    """

    git: bool = False                                      # Git side enabled at all
    repos: list[str] = field(default_factory=list)         # Repository names scanned
    sessions: list[str] = field(default_factory=list)      # Session sources enabled
    dirty: bool = False                                    # Worktree state was scanned
    only_repos: list[str] = field(default_factory=list)    # Strict scope, absolute paths


@dataclass
class Report:
    """The complete result of one collection, feeding both the Markdown and evidence.json."""

    period: Period
    commits: list[Commit] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    truncated: bool = False         # Some sessions have no detailed summary
    extras: int = 0                 # Collapsed rebase/cherry-pick copies
    dirty: list[DirtyState] = field(default_factory=list)
    fetch_results: list[FetchResult] = field(default_factory=list)
    pull_requests: list[PullRequest] = field(default_factory=list)
    pr_diagnostics: PullRequestDiagnostics | None = None   # None = discovery never ran
    diagnostics: dict[str, SourceDiagnostics] = field(default_factory=dict)  # by source name
    scope: Scope | None = None             # None = the run recorded no scope
    stale_days: int = 7             # Threshold for the "no commits for a long time" note
    generated: datetime | None = None
    period_word: str | None = None

    def as_evidence(self) -> dict:
        """Shape of evidence.json; this key order is the order in the file."""
        return {"schema_version": 1, "generated_at": self.generated,
                "start": self.period.start, "end_exclusive": self.period.end,
                "scope": self.scope,
                "diagnostics": self.diagnostics, "git_fetch": self.fetch_results,
                "commits": self.commits, "sessions": self.sessions, "dirty": self.dirty,
                "pull_requests": self.pull_requests, "pr_diagnostics": self.pr_diagnostics}
