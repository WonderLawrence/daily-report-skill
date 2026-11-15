#!/usr/bin/env python3
"""Collect this period's work evidence: parse arguments, resolve settings,
collect, render, write.

Evidence comes from two paths. Commits go through the two level PR traversal:
search the
PRs involved this period, keep the user's commits inside them whose committer
date falls in
the window, then fill in the changes from local repositories by SHA. Sessions
are read from
the local Claude Code / Codex / DSH records.

Usage is documented in README.md and the meaning of the evidence in
references/collection.md; the rest of the implementation lives in the
daily_report package.
Evidence and diagnostics are in Chinese; CLI help is in English.
"""

from __future__ import annotations

import argparse
import json
import sys

from daily_report import messages as m
from daily_report.config import default_config_path
from daily_report.evidence import (
    EvidenceWriteError,
    json_evidence,
    prune_evidence,
    redact_text,
    write_evidence,
)
from daily_report.gitlog import print_identity_report
from daily_report.models import ArtifactKind, Report
from daily_report.pipeline import run_collection
from daily_report.render import render
from daily_report.settings import MissingGit, resolve_settings


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help=m.CLI_HELP_DATE)
    ap.add_argument("--days", type=int, default=None, help=m.CLI_HELP_DAYS)
    week = ap.add_mutually_exclusive_group()
    week.add_argument("--week", action="store_true", help=m.CLI_HELP_WEEK)
    week.add_argument("--last-week", action="store_true", help=m.CLI_HELP_LAST_WEEK)
    ap.add_argument("--config", default=str(default_config_path()), help=m.CLI_HELP_CONFIG)
    ap.add_argument("--repos-root", help=m.CLI_HELP_REPOS_ROOT)
    ap.add_argument("--author", help=m.CLI_HELP_AUTHOR)
    repos = ap.add_mutually_exclusive_group()
    repos.add_argument("--repo", action="append", default=[], help=m.CLI_HELP_REPO)
    repos.add_argument("--only-repo", action="append", help=m.CLI_HELP_ONLY_REPO)
    ap.add_argument("--any-author", action="store_true", help=m.CLI_HELP_ANY_AUTHOR)
    ap.add_argument("--no-git", action="store_true", help=m.CLI_HELP_NO_GIT)
    ap.add_argument(
        "--fetch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=m.CLI_HELP_FETCH,
    )
    ap.add_argument("--fetch-timeout", type=int, help=m.CLI_HELP_FETCH_TIMEOUT)
    ap.add_argument(
        "--prs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=m.CLI_HELP_PRS,
    )
    ap.add_argument("--no-sessions", action="store_true", help=m.CLI_HELP_NO_SESSIONS)
    ap.add_argument("--dirty", action="store_true", help=m.CLI_HELP_DIRTY)
    ap.add_argument("--no-redact", action="store_true", help=m.CLI_HELP_NO_REDACT)
    ap.add_argument("--keep", type=int, help=m.CLI_HELP_KEEP)
    ap.add_argument("--max-sessions", type=int, help=m.CLI_HELP_MAX_SESSIONS)
    ap.add_argument("--whoami", action="store_true", help=m.CLI_HELP_WHOAMI)
    ap.add_argument("--out", help=m.CLI_HELP_OUT)
    ap.add_argument("--overwrite", action="store_true", help=m.CLI_HELP_OVERWRITE)
    ap.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--print", dest="to_stdout", action="store_true", help=m.CLI_HELP_PRINT)
    ap.add_argument("--json", dest="json_output", action="store_true", help=m.CLI_HELP_JSON)
    return ap


def emit(report: Report, output) -> int:
    """Render, redact and deliver to stdout or the default path.

    A failed write never damages existing evidence.
    """
    text = render(report)
    if output.redact:
        text = redact_text(text)
    counts = m.STATUS_COUNTS.format(commits=len(report.commits), sessions=len(report.sessions))
    if output.to_stdout:
        print(text)
        print(m.STATUS_OK_TO_STDOUT.format(counts=counts), file=sys.stderr)
        return 0

    path = output.evidence_path(report.period)
    evidence = json_evidence(report.as_evidence(), output.redact)
    try:
        written = write_evidence(path, text, output.overwrite, evidence)
    except EvidenceWriteError as exc:
        print(m.STATUS_ERROR_PREFIX.format(detail=exc), file=sys.stderr)
        return 2
    if output.json_output:
        print(
            json.dumps(
                {
                    **written.receipt(),
                    "artifact_kind": ArtifactKind.COLLECTION,
                    "generated_at": evidence["generated_at"],
                    "counts": {
                        "commits": len(report.commits),
                        "sessions": len(report.sessions),
                    },
                },
                ensure_ascii=False,
            )
        )
    print(m.STATUS_OK_TO_PATH.format(counts=counts, path=path), file=sys.stderr)
    if output.keep > 0 and path.parent == output.evidence_dir:
        prune_evidence(output.evidence_dir, output.keep)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json_output and (args.to_stdout or args.whoami):
        parser.error(m.CLI_ERR_JSON_INCOMPATIBLE)
    try:
        settings = resolve_settings(args)
    except MissingGit as e:
        print(m.STATUS_ERROR_PREFIX.format(detail=e), file=sys.stderr)
        return 2
    except ValueError as e:
        parser.error(str(e))
    if args.whoami:
        print_identity_report(
            settings.repos,
            settings.repos_root,
            settings.author_emails,
            settings.identity,
        )
        return 0
    return emit(run_collection(settings), settings.output)


if __name__ == "__main__":
    raise SystemExit(main())
