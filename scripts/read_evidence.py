#!/usr/bin/env python3
"""Read saved collection or PR evidence as bounded JSON pages; never collects or
writes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from daily_report import messages as m
from daily_report.evidence_reader import (
    DEFAULT_MAX_CHARS,
    DEFAULT_PAGE_CHARS,
    DEFAULT_PAGE_ITEMS,
    MIN_MAX_CHARS,
    encode,
    read_page,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=m.CLI_READ_DESCRIPTION,
        epilog=m.CLI_READ_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--evidence", required=True, type=Path, help=m.CLI_HELP_READ_EVIDENCE)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--pr", help=m.CLI_HELP_READ_PR)
    target.add_argument("--session", help=m.CLI_HELP_READ_SESSION)
    parser.add_argument("--field", help=m.CLI_HELP_READ_FIELD)
    parser.add_argument("--fields", help=m.CLI_HELP_READ_FIELDS)
    parser.add_argument("--offset", type=int, default=0, help=m.CLI_HELP_READ_OFFSET)
    parser.add_argument(
        "--limit",
        type=int,
        help=m.CLI_HELP_READ_LIMIT.format(page_items=DEFAULT_PAGE_ITEMS, page_chars=DEFAULT_PAGE_CHARS),
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=m.CLI_HELP_READ_MAX_CHARS.format(min_chars=MIN_MAX_CHARS),
    )
    args = parser.parse_args(argv)
    try:
        result = read_page(
            args.evidence,
            pr=args.pr,
            session=args.session,
            field=args.field,
            projection=[n.strip() for n in args.fields.split(",")] if args.fields is not None else None,
            offset=args.offset,
            limit=args.limit,
            max_chars=args.max_chars,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(encode(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
