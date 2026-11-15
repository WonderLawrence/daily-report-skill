#!/usr/bin/env python3
"""Read the description and commit information of selected pull requests.

Only PRs already present in the collection snapshot are accepted, so no number
is ever
guessed, and no diff is fetched: the change itself is reviewed on demand by SHA.
Usage is documented in README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from daily_report import messages as m
from daily_report.models import ArtifactKind, DetailsReport, DetailsStatus
from daily_report.evidence import EvidenceWriteError, json_evidence
from daily_report.pr_report import (
    UnknownPullRequest,
    fetch_details,
    load_selection,
    write_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=m.CLI_DETAILS_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=m.CLI_DETAILS_EPILOG,
    )
    parser.add_argument("--evidence", required=True, help=m.CLI_HELP_DETAILS_EVIDENCE)
    parser.add_argument("--pr", action="append", required=True, help=m.CLI_HELP_DETAILS_PR)
    parser.add_argument("--out", required=True, help=m.CLI_HELP_DETAILS_OUT)
    parser.add_argument("--timeout", type=int, default=30, help=m.CLI_HELP_DETAILS_TIMEOUT)
    parser.add_argument("--overwrite", action="store_true", help=m.CLI_HELP_DETAILS_OVERWRITE)
    parser.add_argument("--force", dest="overwrite", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help=m.CLI_HELP_DETAILS_JSON,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error(m.CLI_ERR_TIMEOUT_MIN)
    source = Path(args.evidence).expanduser().resolve()
    out = Path(args.out).expanduser()
    if out.exists() and not args.overwrite:
        parser.error(m.CLI_ERR_OUT_EXISTS)
    try:
        snapshot, chosen = load_selection(source, args.pr)
    except UnknownPullRequest as exc:
        parser.error(str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(m.CLI_ERR_LOAD_SNAPSHOT.format(detail=exc))

    details, failures = fetch_details(chosen, args.timeout)
    report = DetailsReport(snapshot, list(chosen), details, failures)
    try:
        written = write_report(out, report, args.overwrite)
    except EvidenceWriteError as exc:
        parser.error(str(exc))
    summary = report.summary
    if args.json_output:
        print(
            json.dumps(
                {
                    **written.receipt(),
                    "artifact_kind": ArtifactKind.PR_DETAILS,
                    "status": report.status,
                    "summary": json_evidence(summary, False),
                },
                ensure_ascii=False,
            )
        )
    log = sys.stderr if args.json_output else sys.stdout
    print(
        m.STATUS_DETAILS_HEADER.format(
            status=report.status,
            loaded=len(details),
            requested=len(chosen),
            out=out,
        ),
        file=log,
    )
    print(m.STATUS_DETAILS_SUMMARY.format_map(vars(summary)), file=log)
    if report.status == DetailsStatus.PARTIAL:
        print(m.STATUS_DETAILS_WARN, file=log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
