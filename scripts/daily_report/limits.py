"""Truncation and excerpt budgets, in one place so writers and readers stay in sync.

Session summaries and the Markdown renderer both shorten the same strings; keeping the
limits here prevents one side from quietly diverging from the other and makes the
output budgets reviewable without grepping the codebase.
"""
from __future__ import annotations


# --- one_line() budgets by kind of content ---
USER_REQUEST = 220              # User-request text in Session.users and event excerpts
ASSISTANT_STATEMENT = 260       # Assistant-statement text in Session.concl and event excerpts
SESSION_HINT = 160              # Session-index hint in the renderer
PR_TITLE = 180                  # PR titles in render / pr_details
COMMIT_MESSAGE = 600            # Full commit message in pr-details
FETCH_FAILURE = 800             # fetch failure stderr excerpt
EVENT_EXCERPT = 260             # Default limit for event_excerpt()

# --- head/tail sample sizes the renderer or aggregator keeps ---
CONCL_TAIL = 3                  # Last N assistant statements kept on Session.concl
RELATED_COMMITS = 3             # Max commit hints attached to a session
TOPIC_SCOPES = 8                # Top N commit scopes in scope_summary
TOPIC_TYPES = 6                 # Top N commit types shown per scope row
TOOLS_SHOWN = 5                 # Most-common tools shown per session detail
FILES_SHOWN = 5                 # File paths listed per commit entry
REL_PATHS = 4                   # Relative paths shown per session detail

# --- squeeze head+tail for long event lists ---
USER_HEAD = 2
USER_TAIL = 2
ASSISTANT_TAIL = 2
TOOL_RESULT_TAIL = 3

# --- auxiliary ---
IDENTITY_AUTHORS = 4            # Authors shown per repository in --whoami
FETCH_FAILURE_LINES = 5         # Last N stderr lines kept after redaction
ISSUE_RECORD_TYPE = 80          # SessionIssue.record_type truncation
