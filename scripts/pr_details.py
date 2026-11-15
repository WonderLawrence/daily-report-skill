#!/usr/bin/env python3
"""Read the description and commit information of selected pull requests.

Only PRs already present in the collection snapshot are accepted, so no number is ever
guessed, and no diff is fetched: the change itself is reviewed on demand by SHA.
Usage is documented in README.md.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from daily_report.models import DetailsReport, MessageStatus
from daily_report.pr_report import (UnknownPullRequest, fetch_details, load_selection,
                                    write_report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, help="采集快照中的 evidence.json")
    parser.add_argument("--pr", action="append", required=True,
                        help="已选 PR 的 owner/repo#编号或完整链接，可重复")
    parser.add_argument("--out", required=True, help="保存 PR 分析证据的 Markdown 路径")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout 必须大于等于 1")
    source = Path(args.evidence).expanduser().resolve()
    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        parser.error("输出已存在；需要更新时加 --force 保存新快照")
    try:
        snapshot, chosen = load_selection(source, args.pr)
    except UnknownPullRequest as exc:
        parser.error(str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(f"无法读取有效采集快照：{exc}")

    details, failures = fetch_details(chosen, args.timeout)
    report = DetailsReport(snapshot, list(chosen), details, failures)
    error = write_report(out, report, args.force)
    if error:
        parser.error(error)
    print(f"[ok] {len(details)} PR details -> {out}")
    if failures or any(commit.message_status != MessageStatus.OK
                       for pr in details for commit in pr.period_commits):
        print("[warn] 部分 PR 详情或提交信息不完整；已保存可得证据，不能据此声称完整覆盖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
