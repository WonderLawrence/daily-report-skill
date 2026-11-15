"""Configuration defaults, loading and validation.

Path resolution happens when called, never by reading the environment at import
time.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import messages as m

# scripts/daily_report/config.py -> scripts -> skill root
SKILL_DIR = Path(__file__).resolve().parents[2]

DEFAULTS = {
    # Empty = fall back to git config user.email; list every identity
    "author_emails": [],
    "repos_root": "~/Project",
    # When set, replaces repository discovery and filters sessions
    "only_repos": [],
    "reports_dir": "~/Documents/reports",
    "utc_offset_hours": 8,
    # Detailed summary budget; index and JSON are never truncated
    "max_sessions": 30,
    "stale_days": 7,
    "fetch": True,  # Update remote tracking refs before reading commits
    "fetch_timeout_seconds": 30,
    "github_prs": True,
    "github_timeout_seconds": 30,
    "github_max_queries": 200,
    # Redact tokens, private keys and internal IPs before writing
    "redact": True,
    # Keep the N newest latest views; never prunes history snapshots
    "evidence_keep": 30,
    "sources": {"git": True, "claude": True, "codex": True, "dsh": True},
}

# Key -> minimum value; only checked when the key is present
INTEGER_RANGES = {
    "max_sessions": 1,
    "evidence_keep": 0,
    "stale_days": 0,
    "fetch_timeout_seconds": 1,
    "github_timeout_seconds": 1,
    "github_max_queries": 1,
}
BOOLEAN_KEYS = ("dirty", "redact", "fetch", "github_prs")


def default_config_path() -> Path:
    """Resolve DAILY_REPORT_CONFIG, then the user config, then a workspace
    config.
    """
    override = os.environ.get("DAILY_REPORT_CONFIG")
    if override:
        return Path(override).expanduser()
    standard = Path.home() / ".config" / "daily-report" / "config.json"
    workspace = SKILL_DIR.parent / "reports" / "daily-report.config.json"
    if standard.exists() or not workspace.exists():
        return standard
    # Support workspace-local setups that keep the report config beside the
    # reports directory.
    return workspace


def load_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(m.CFG_ERR_LOAD_FAILED.format(detail=e)) from e
        if not isinstance(user, dict):
            raise ValueError(m.CFG_ERR_TOP_LEVEL)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def _is_path_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def validate_config(cfg: dict) -> list[str]:
    """Return actionable errors for values that would otherwise fail mid-run."""
    errors = []
    if not _is_path_list(cfg.get("author_emails")):
        errors.append(m.CFG_ERR_AUTHOR_EMAILS)
    if not _is_path_list(cfg.get("only_repos", [])):
        errors.append(m.CFG_ERR_ONLY_REPOS)
    for key in ("repos_root", "reports_dir"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            errors.append(m.CFG_ERR_PATH_STRING.format(key=key))
    offset = cfg.get("utc_offset_hours", 8)
    if not _is_number(offset):
        errors.append(m.CFG_ERR_UTC_OFFSET_TYPE)
    elif not -12 <= float(offset) <= 14:
        errors.append(m.CFG_ERR_UTC_OFFSET_RANGE)
    for key, minimum in INTEGER_RANGES.items():
        value = cfg.get(key, minimum)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(m.CFG_ERR_INT_RANGE.format(key=key, minimum=minimum))
    for key in BOOLEAN_KEYS:
        if key in cfg and not isinstance(cfg[key], bool):
            errors.append(m.CFG_ERR_BOOL.format(key=key))
    sources = cfg.get("sources")
    if not isinstance(sources, dict) or any(not isinstance(value, bool) for value in sources.values()):
        errors.append(m.CFG_ERR_SOURCES)
    return errors
