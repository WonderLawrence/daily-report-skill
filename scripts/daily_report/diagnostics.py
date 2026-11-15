"""The single definition of collection diagnostics.

Diagnostics answer "which work might not have been collected", so they must not live as
string keys scattered across modules: the writers (gitlog / sessions / github / pipeline)
and the readers (render / evidence.json) share the fields below, and a typo raises instead
of silently printing 0 in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class SourceDiagnostics:
    """File level counts for any source.

    Successes and failures are counted apart; a missing source does not mean no work.
    """

    scanned: int = 0                # Files or repositories actually read
    discovered: int = 0             # Candidates seen before the mtime prefilter
    ok_files: int = 0               # No known parse error
    failed_files: int = 0           # Incomplete read or parse
    invalid_records: int = 0        # Damaged records
    read_errors: int = 0
    unrecognized_files: int = 0     # Has content but no recognizable format, usually drift
    disabled: bool = False
    unavailable: bool = False       # Directory or dependency missing

    @property
    def has_records(self) -> bool:
        return False

    @property
    def has_fetch_gap(self) -> bool:
        """Only sources with remotes can have an incomplete fetch."""
        return False

    def merge(self, other: SourceDiagnostics) -> None:
        """Fold one file's local counts into the source level counts."""
        for item in fields(other):
            value = getattr(other, item.name)
            if isinstance(value, bool):
                if value:
                    setattr(self, item.name, True)
            elif isinstance(value, int):
                setattr(self, item.name, getattr(self, item.name) + value)


@dataclass
class GitDiagnostics(SourceDiagnostics):
    """Git side: fetch results, plus the gap between PR evidence and the local repository."""

    fetch_ok: int = 0
    fetch_failed: int = 0
    fetch_skipped: int = 0
    commits: int = 0                        # Commits that entered the evidence
    local_commits: int = 0                  # The user's commits found by the local scan
    local_unassociated: int = 0             # ...not found in this run's PR results
    commits_without_local_copy: int = 0     # PR commits absent locally: platform metadata only

    @property
    def has_records(self) -> bool:
        return bool(self.commits)

    @property
    def has_fetch_gap(self) -> bool:
        return bool(self.fetch_failed)


@dataclass
class SessionDiagnostics(SourceDiagnostics):
    """Session side: parse progress and scope filtering; the summary budget drops nothing."""

    files: int = 0                  # Session files modified inside the window
    timestamps: int = 0             # Records with a parsable timestamp
    events: int = 0                 # Records inside the window
    sessions: int = 0
    detailed: int = 0               # Expanded into a detailed summary
    omitted: int = 0                # Summary omitted; still in the index and the JSON
    scope_excluded: int = 0         # Known to be outside the selected scope
    scope_unknown: int = 0          # Project could not be determined
    user_messages: int = 0          # Extracted text events, before project scope filtering
    assistant_messages: int = 0
    unparsed_messages: int = 0      # Unsupported message format or unreadable content blocks

    @property
    def has_records(self) -> bool:
        return bool(self.sessions)


class CoverageStatus:
    """How complete a source's coverage is; the values are the on-disk form."""

    OK = "ok"
    PARTIAL = "partial"            # Some queries failed or were skipped
    UNAVAILABLE = "unavailable"    # Dependency missing or the search itself failed
    DISABLED = "disabled"


@dataclass
class PullRequestDiagnostics:
    """Coverage of the two level PR traversal: no results does not mean no PR work."""

    status: str = CoverageStatus.OK
    queries: int = 0
    failures: int = 0
    unqueried: int = 0              # Skipped by the query budget or cut off by rate limiting
    prs_found: int = 0
    prs_with_commits: int = 0
    prs_without_commits: int = 0
    out_of_scope: int = 0           # No local repository, while a strict scope was given
    unsupported_repos: list[str] = field(default_factory=list)  # Repositories not cloned locally
    detail: str = ""
    checked_at: str = ""
    search_truncated: bool = False  # Search cap, short page or query budget hid candidates
    search_incomplete: bool = False # GitHub returned incomplete_results=true
    searches_skipped: int = 0       # Repository searches never started
    commit_lists_capped: int = 0    # PR lists hit the platform's 250-commit boundary
