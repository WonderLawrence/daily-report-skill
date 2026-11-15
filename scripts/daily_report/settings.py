"""Merge command line arguments and configuration into one run setting.

Collection reads this object alone and never goes back to args or the config dict.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_config, validate_config
from .github import repo_index
from .gitlog import author_regex, discover_repos, git_config_value
from .models import Identity
from .sessions import SESSION_SOURCES
from .timewindow import Period, resolve_period, timezone_from_offset

SESSION_NAMES = tuple(source.name for source in SESSION_SOURCES)


class MissingGit(Exception):
    """git is missing from PATH: stop before reading any repository, not halfway through."""


@dataclass(frozen=True)
class GitOptions:
    """Git side switches: fetch decides whether a PR's SHAs can be resolved locally."""

    enabled: bool = True
    fetch: bool = True
    fetch_timeout: int = 30


@dataclass(frozen=True)
class GitHubOptions:
    """Switch and budget for the two level PR traversal; disabled entirely when offline."""

    enabled: bool = True
    timeout: int = 30
    max_queries: int = 200


@dataclass(frozen=True)
class SessionOptions:
    """Session sources and the detailed summary budget, which never truncates the index."""

    sources: frozenset[str] = frozenset()
    limit: int = 30


@dataclass(frozen=True)
class OutputOptions:
    """Delivery: output path, overwrite permission, redaction and retention."""

    path: Path | None = None
    to_stdout: bool = False
    force: bool = False
    redact: bool = True
    keep: int = 30
    reports_dir: Path = Path()

    @property
    def evidence_dir(self) -> Path:
        return self.reports_dir / ".evidence"

    def evidence_path(self, period: Period) -> Path:
        """Default latest view: <YYYYMMDD>.md for one day, <YYYYMMDD>-<N>d.md for more."""
        if self.path:
            return self.path
        name = f"{period.start:%Y%m%d}" + (f"-{period.days}d" if period.days > 1 else "") + ".md"
        return self.evidence_dir / name


@dataclass
class Settings:
    """Everything one collection needs, grouped by concern."""

    period: Period
    identity: Identity
    repos: list[Path] = field(default_factory=list)
    repo_index: dict[str, Path] = field(default_factory=dict)
    project_roots: list[Path] | None = None     # --only-repo / only_repos: a strict scope
    repos_root: Path = Path()
    author_emails: list[str] = field(default_factory=list)
    git: GitOptions = GitOptions()
    github: GitHubOptions = GitHubOptions()
    sessions: SessionOptions = SessionOptions()
    output: OutputOptions = OutputOptions()
    dirty_configured: bool = False
    dirty_explicit: bool = False
    stale_days: int = 7
    period_word: str | None = None

    @property
    def scoped(self) -> bool:
        return self.project_roots is not None


def _pick(cli_value, config_value):
    """An explicit CLI value wins; otherwise fall back to the configuration."""
    return config_value if cli_value is None else cli_value


def _resolve_scope(args, cfg: dict, git_enabled: bool) -> tuple[list[Path], list[Path] | None]:
    """--only-repo / only_repos is a strict scope; --repo only appends. They are exclusive."""
    only = args.only_repo if args.only_repo is not None else cfg["only_repos"]
    if only and args.repo:
        raise ValueError("--repo 不能与 only_repos 配置同时使用；请用 --only-repo 指定完整范围")
    roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in only)) if only else None
    for path in roots or []:
        if not path.is_dir():
            raise ValueError(f"关注项目路径不存在或不是目录：{path}")
        if git_enabled:
            result = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True)
            if result.returncode or Path(result.stdout.strip()).resolve() != path:
                raise ValueError(f"--only-repo / only_repos 必须指向仓库根目录：{path}")
    if not git_enabled:
        return [], roots
    repos = roots if roots is not None else (
        [Path(p).expanduser().resolve() for p in args.repo]
        + discover_repos(Path(args.repos_root or cfg["repos_root"]).expanduser().resolve()))
    return list(dict.fromkeys(path.resolve() for path in repos)), roots


def resolve_settings(args) -> Settings:
    """Merge arguments and configuration; unusable values raise ValueError for exit code 2."""
    if args.days is not None and args.days < 1:
        raise ValueError("--days 必须大于等于 1")
    if (args.week or args.last_week) and (args.date or args.days is not None):
        raise ValueError("--week / --last-week 不能和 --date / --days 同时使用")

    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"[warn] 配置文件不存在，使用默认值：{config_path}", file=sys.stderr)
    cfg = load_config(config_path)
    errors = validate_config(cfg)
    if errors:
        raise ValueError("配置无效：" + "；".join(errors))

    limit = _pick(args.max_sessions, cfg["max_sessions"])
    if limit < 1:
        raise ValueError("--max-sessions / config.max_sessions 必须大于等于 1")
    keep = _pick(args.keep, int(cfg.get("evidence_keep", 30)))
    if keep < 0:
        raise ValueError("--keep 必须大于等于 0")
    fetch_timeout = _pick(args.fetch_timeout, cfg["fetch_timeout_seconds"])
    if fetch_timeout < 1:
        raise ValueError("--fetch-timeout 必须大于等于 1")

    tz = timezone_from_offset(float(cfg["utc_offset_hours"]))
    period = resolve_period(args.date, 1 if args.days is None else args.days, tz,
                            week="this" if args.week else "last" if args.last_week else None)
    email = git_config_value("user.email")
    emails = cfg["author_emails"] or [email]
    identity = Identity(author_regex(args.author, emails), args.any_author,
                        git_config_value("user.name"), email,
                        author_explicit=args.author is not None)

    sources = cfg.get("sources", {})
    git_enabled = not args.no_git and sources.get("git", True)
    if git_enabled and not shutil.which("git"):
        raise MissingGit("找不到 git 命令，请确认已安装并在 PATH 中")
    repos, project_roots = _resolve_scope(args, cfg, git_enabled)
    fetch = _pick(args.fetch, cfg["fetch"])
    github_timeout = cfg["github_timeout_seconds"]

    return Settings(
        period=period, identity=identity, repos=repos, project_roots=project_roots,
        repo_index=repo_index(repos, github_timeout) if git_enabled else {},
        repos_root=Path(args.repos_root or cfg["repos_root"]).expanduser().resolve(),
        author_emails=cfg["author_emails"],
        git=GitOptions(git_enabled, fetch, fetch_timeout),
        # Offline collection never queries GitHub: --prs does not override --no-fetch
        github=GitHubOptions(_pick(args.prs, cfg["github_prs"]) and fetch and git_enabled,
                             github_timeout, cfg["github_max_queries"]),
        sessions=SessionOptions(
            frozenset() if args.no_sessions else
            frozenset(name for name in SESSION_NAMES if sources.get(name, True)), limit),
        output=OutputOptions(
            Path(args.out).expanduser() if args.out else None, args.to_stdout, args.force,
            (not args.no_redact) and bool(cfg.get("redact", True)), keep,
            Path(cfg["reports_dir"]).expanduser()),
        dirty_configured=bool(cfg.get("dirty")), dirty_explicit=args.dirty,
        stale_days=int(cfg.get("stale_days", 7)),
        period_word="周报" if args.week or args.last_week else None)
