#!/usr/bin/env python3
"""Collect this period's work evidence: parse arguments, resolve settings, collect, render, write.

Evidence comes from two paths. Commits go through the two level PR traversal: search the
PRs involved this period, keep the user's commits inside them whose committer date falls in
the window, then fill in the changes from local repositories by SHA. Sessions are read from
the local Claude Code / Codex / DSH records.

Usage is documented in README.md and the meaning of the evidence in
references/collection.md; the rest of the implementation lives in the daily_report package.
Text printed for the user stays in Chinese: it is the product.
"""
from __future__ import annotations

import argparse
import sys

from daily_report.config import default_config_path
from daily_report.evidence import json_evidence, prune_evidence, redact_text, write_evidence
from daily_report.gitlog import print_identity_report
from daily_report.models import Report
from daily_report.pipeline import run_collection
from daily_report.render import render
from daily_report.settings import MissingGit, resolve_settings


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="区间结束日 YYYY-MM-DD，也可写 today / yesterday；默认今天")
    ap.add_argument("--days", type=int, default=None,
                    help="包含结束日在内向前取几天（默认 1；最近 7 天用 7）")
    week = ap.add_mutually_exclusive_group()
    week.add_argument("--week", action="store_true", help="本周：本周一至今天（不能和 --date/--days 同用）")
    week.add_argument("--last-week", action="store_true",
                      help="上周：上周一至上周日（不能和 --date/--days 同用）")
    ap.add_argument("--config", default=str(default_config_path()),
                    help="配置文件（默认 DAILY_REPORT_CONFIG、用户配置或工作区配置）")
    ap.add_argument("--repos-root", help="覆盖 config 的 repos_root")
    ap.add_argument("--author", help="按作者正则筛选提交，覆盖邮箱及 GitHub login 匹配，多身份用 'a|b'")
    repos = ap.add_mutually_exclusive_group()
    repos.add_argument("--repo", action="append", default=[], help="追加仓库，仍扫描默认根目录，可重复")
    repos.add_argument("--only-repo", action="append", help="仅采集这些仓库及其项目会话，可重复")
    ap.add_argument("--any-author", action="store_true", help="不过滤提交作者；PR 搜索仍限定登录账号")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--fetch", action=argparse.BooleanOptionalAction, default=None,
                    help="采集前 fetch 所有配置远程（默认开启；--no-fetch 离线采集）")
    ap.add_argument("--fetch-timeout", type=int, help="每次 Git 同步操作的超时秒数（默认 30）")
    ap.add_argument("--prs", action=argparse.BooleanOptionalAction, default=None,
                    help="按 PR 发现本期提交（默认开启；--no-fetch 离线时也禁用）")
    ap.add_argument("--no-sessions", action="store_true")
    ap.add_argument("--dirty", action="store_true",
                    help="附带当前未提交改动（tracked 文件），不推断改动年龄")
    ap.add_argument("--no-redact", action="store_true", help="关闭证据包脱敏")
    ap.add_argument("--keep", type=int, help="最新视图保留份数，0 = 不清理；不删除历史快照")
    ap.add_argument("--max-sessions", type=int, help="详细摘要额度；完整会话索引和 JSON 不截断")
    ap.add_argument("--whoami", action="store_true",
                    help="只做身份体检：打印 git 身份、配置里的邮箱、各仓库近期作者")
    ap.add_argument("--out", help="证据包输出路径")
    ap.add_argument("--force", action="store_true", help="保存独立快照后更新已存在的最新视图")
    ap.add_argument("--print", dest="to_stdout", action="store_true", help="打到 stdout")
    return ap


def emit(report: Report, output) -> int:
    """Render, redact and deliver to stdout or the default path.

    A failed write never damages existing evidence.
    """
    text = render(report)
    if output.redact:
        text = redact_text(text)
    counts = f"{len(report.commits)} commits, {len(report.sessions)} sessions"
    if output.to_stdout:
        print(text)
        print(f"[ok] {counts}", file=sys.stderr)
        return 0

    path = output.evidence_path(report.period)
    evidence = json_evidence(report.as_evidence(), output.redact)
    write_error = write_evidence(path, text, output.force, evidence)
    if write_error:
        print(f"[error] {write_error}", file=sys.stderr)
        return 2
    print(f"[ok] {counts} -> {path}", file=sys.stderr)
    if output.keep > 0 and path.parent == output.evidence_dir:
        prune_evidence(output.evidence_dir, output.keep)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = resolve_settings(args)
    except MissingGit as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        parser.error(str(e))
    if args.whoami:
        print_identity_report(settings.repos, settings.repos_root,
                              settings.author_emails, settings.identity)
        return 0
    return emit(run_collection(settings), settings.output)


if __name__ == "__main__":
    raise SystemExit(main())
