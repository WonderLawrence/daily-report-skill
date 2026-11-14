#!/usr/bin/env python3
"""collect.py — 采集一天（或 N 天）的「Git 提交 + Agent 会话」证据，输出喂给 LLM 写日报的 Markdown。

只依赖标准库；DSH（DeepSeek Harness）会话是 zstd 压缩，走 `zstd -dc`，没装就自动跳过。

    python3 collect.py                          # 今天
    python3 collect.py --date 2026-10-16        # 指定日（作为区间结束日）
    python3 collect.py --days 7                 # 最近 7 天（周报）
    python3 collect.py --date 2026-10-16 --no-sessions
    python3 collect.py --print                  # 打到 stdout 而不是写文件

日期语义：按 **committer date**（提交落地时间）判断归属。rebase 会保留 author date、
改写 committer date，所以"几天前写、这周落地"的工作算本周；两者相差超过一天时，
证据里会额外标注作者时间。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
STANDARD_CONFIG = HOME / ".config" / "daily-report" / "config.json"
WORKSPACE_CONFIG = Path(__file__).resolve().parents[2] / "reports" / "daily-report.config.json"
if os.environ.get("DAILY_REPORT_CONFIG"):
    DEFAULT_CONFIG = Path(os.environ["DAILY_REPORT_CONFIG"]).expanduser()
elif STANDARD_CONFIG.exists() or not WORKSPACE_CONFIG.exists():
    DEFAULT_CONFIG = STANDARD_CONFIG
else:
    # Support workspace-local setups that keep the report config beside the reports directory.
    DEFAULT_CONFIG = WORKSPACE_CONFIG

DEFAULTS = {
    "author_emails": [],          # 空 = 用 git config user.email；多个身份写全
    "repos_root": "~/Project",
    "reports_dir": "~/Documents/reports",
    "utc_offset_hours": 8,
    "max_sessions": 30,           # 详细摘要额度；完整索引和结构化证据不截断
    "stale_days": 7,
    "redact": True,               # 证据落盘前脱敏（token/私钥/内网 IP）
    "evidence_keep": 30,          # 保留最近 N 份最新视图；不清理历史快照
    "sources": {"git": True, "claude": True, "codex": True, "dsh": True},
}

COMMIT_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<rest>.*)$")


# ---------------- 基础工具 ----------------
def load_config(path: Path) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"配置文件解析失败：{e}") from e
        if not isinstance(user, dict):
            raise ValueError("配置文件顶层必须是 JSON 对象")
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Return actionable errors for values that would otherwise fail mid-run."""
    errors = []
    emails = cfg.get("author_emails")
    if (not isinstance(emails, list)
            or any(not isinstance(value, str) or not value.strip() for value in emails)):
        errors.append("author_emails 必须是非空邮箱组成的字符串数组（允许整个数组为空）")
    for key in ("repos_root", "reports_dir"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            errors.append(f"{key} 必须是非空路径字符串")
    try:
        raw_offset = cfg.get("utc_offset_hours", 8)
        if isinstance(raw_offset, bool):
            raise ValueError
        offset = float(raw_offset)
        if not -12 <= offset <= 14:
            errors.append("utc_offset_hours 必须在 -12 到 14 之间")
    except (TypeError, ValueError):
        errors.append("utc_offset_hours 必须是数字")
    for key, minimum in (("max_sessions", 1), ("evidence_keep", 0), ("stale_days", 0)):
        if key not in cfg:
            continue
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(f"{key} 必须是大于等于 {minimum} 的整数")
    for key in ("dirty", "redact"):
        if key in cfg and not isinstance(cfg[key], bool):
            errors.append(f"{key} 必须是布尔值")
    sources = cfg.get("sources")
    if not isinstance(sources, dict) or any(not isinstance(value, bool) for value in sources.values()):
        errors.append("sources 必须是由布尔值组成的对象")
    return errors


def parse_ts(s: str, tz: timezone) -> datetime | None:
    if not s:
        return None
    try:
        value = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz)
    except ValueError:
        return None


def epoch_ms(ms, tz: timezone) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def one_line(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def blocks_text(content) -> str:
    """从 content（str 或 block 数组）里只取可见正文，丢掉 thinking/tool_use。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def user_text(content) -> str:
    """用户消息的可见正文。block 数组逐块剔除注入块再拼接：
    IDE 上下文（<ide_opened_file> 等）和真实请求常在同一条消息里，整条判断会把请求一并吞掉。"""
    if isinstance(content, list):
        parts = [b.get("text", "").strip() for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if not is_injected(p))
    return blocks_text(content).strip()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# skill 展开、subagent 回传、系统注入都会伪装成"用户消息"，写日报时是噪音
NOISE_PREFIXES = (
    "Base directory for this skill",
    "Caveat:",
    "Another Claude session sent a message",
    "This session is being continued",
    "<",
)


def is_injected(txt: str) -> bool:
    if not txt:
        return True
    if txt.startswith(NOISE_PREFIXES):
        return True
    return "<agent-message" in txt or "This session is being continued" in txt[:200]


# ---------------- 脱敏 ----------------
REDACT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-pat]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "[REDACTED:private-key]"),
    (re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "[REDACTED:private-ip]"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "[REDACTED:private-ip]"),
    (re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"), "[REDACTED:private-ip]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b(\s*[=:]\s*)(\S{6,})"),
     r"\1\2[REDACTED]"),
]


def redact_text(text: str) -> str:
    """证据包落盘前的兜底脱敏；不追求完备，只挡最常见的高危形态。"""
    for pat, repl in REDACT_RULES:
        text = pat.sub(repl, text)
    return text


# ---------------- 1. Git ----------------
def discover_repos(root: Path, depth: int = 2) -> list[Path]:
    found: set[Path] = set()
    root = root.expanduser()
    if not root.is_dir():
        return []
    for d in range(1, depth + 1):
        for p in root.glob("/".join(["*"] * d)):
            if p.is_dir() and (p / ".git").exists():
                found.add(p)
    return sorted(found)


def git_commits(repo: Path, start, end, diag: Counter | None = None) -> list[dict]:
    """读取日期区间内的提交，按 committer date 归属。"""
    fmt = "%H%x1f%h%x1f%cd%x1f%ad%x1f%an <%ae>%x1f%s"
    base = ["git", "-C", str(repo), "log", "--exclude=refs/stash", "--all", "--no-merges"]
    tail = ["--date=iso-strict", f"--pretty=format:{fmt}", "--numstat"]
    since_filter = f"--since-as-filter={start.isoformat()}"
    result = subprocess.run(base + [since_filter] + tail, capture_output=True, text=True)

    if result.returncode != 0 and "since-as-filter" in (result.stderr or ""):
        # Older Git versions lack --since-as-filter. Avoid --since here: its history
        # pruning can silently omit commits when committer dates are non-monotonic.
        print(f"[note] {repo.name}: Git 不支持 --since-as-filter，改为遍历完整历史后过滤日期",
              file=sys.stderr)
        result = subprocess.run(base + tail, capture_output=True, text=True)
    if result.returncode != 0:
        if diag is not None:
            diag["failed_files"] += 1
        print(f"[warn] 无法读取仓库 {repo}: {(result.stderr or '').strip()}", file=sys.stderr)
        return []
    if diag is not None:
        diag["ok_files"] += 1
    if not result.stdout.strip():
        return []

    commits: list[dict] = []
    current = None
    for line in result.stdout.splitlines():
        if "\x1f" in line:
            full, short, committed, authored, who, subject = line.split("\x1f", 5)
            current = {
                "repo": repo.name,
                "repo_path": str(repo),
                "full_sha": full,
                "sha": short,
                "at": committed,
                "authored": authored,
                "author": who,
                "subject": subject,
                "files": [],
                "added": 0,
                "deleted": 0,
                "revert": subject.startswith('Revert "'),
            }
            commits.append(current)
        elif current is not None and line.strip():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            current["files"].append(path)
            current["added"] += int(added) if added.isdigit() else 0
            current["deleted"] += int(deleted) if deleted.isdigit() else 0

    # Always apply both bounds locally, including on the --since-as-filter path.
    kept = []
    for commit in commits:
        timestamp = parse_ts(commit["at"], timezone.utc)
        if timestamp is not None and start <= timestamp < end:
            kept.append(commit)
    _read_revert_targets(repo, kept)
    return kept


def _read_revert_targets(repo: Path, commits: list[dict]) -> None:
    """Read the target SHA from Git's revert body for reliable pairing."""
    for commit in commits:
        if not commit["revert"]:
            continue
        result = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%B", commit["full_sha"]],
            capture_output=True,
            text=True,
        )
        match = re.search(r"(?im)^This reverts commit ([0-9a-f]{7,40})\.", result.stdout)
        if result.returncode == 0 and match:
            commit["revert_target"] = match.group(1)


def git_patch_id(repo: Path, sha: str) -> str | None:
    """Return a stable patch identity, or None when Git cannot produce one."""
    show = subprocess.run(
        ["git", "-C", str(repo), "show", "--pretty=format:", "--no-ext-diff", "--binary", sha],
        capture_output=True,
    )
    if show.returncode != 0 or not show.stdout:
        return None
    # show.stdout is bytes (--binary diffs aren't valid text), so stay in bytes mode here.
    patch = subprocess.run(["git", "patch-id", "--stable"], input=show.stdout,
                           capture_output=True)
    if patch.returncode != 0 or not patch.stdout.strip():
        return None
    return patch.stdout.decode("ascii", "replace").split()[0]


def dirty_state(repo: Path, now: datetime) -> dict | None:
    """未提交改动：只看 tracked 文件，顺带给出最后一次提交距今多少天。"""
    r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-uno"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    files = [l for l in r.stdout.splitlines() if l.strip()]
    if not files:
        return None
    last = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%ct"],
                          capture_output=True, text=True).stdout.strip()
    age = None
    if last.isdigit():
        age = (now - datetime.fromtimestamp(int(last), now.tzinfo)).days
    return {"repo": repo.name, "files": len(files), "age_days": age}


# ---------------- 2. Claude Code ----------------
_PROJECT_MAP: dict[str, str] | None = None


def claude_project_map() -> dict[str, str]:
    """slug 目录 -> 真实 cwd（同目录下的子 agent 文件常常没有 cwd 字段）。"""
    global _PROJECT_MAP
    if _PROJECT_MAP is not None:
        return _PROJECT_MAP
    _PROJECT_MAP = {}
    base = HOME / ".claude" / "projects"
    for f in base.glob("*/*.jsonl"):
        if f.parent.name in _PROJECT_MAP:
            continue
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(d, dict) and isinstance(d.get("cwd"), str):
                        _PROJECT_MAP[f.parent.name] = d["cwd"]
                        break
        except OSError:
            continue
    return _PROJECT_MAP


def _scan_candidate(path: Path, start, end, diag: Counter | None) -> bool:
    """Prefilter by mtime; copied files with unreliable mtimes may need manual review."""
    mtime = _mtime(path)
    if diag is not None:
        diag["discovered"] += 1
    if mtime is None:
        if diag is not None:
            diag["scanned"] += 1
            diag["failed_files"] += 1
        return False
    if mtime < start.timestamp() - 86400:
        return False
    if diag is not None:
        diag["scanned"] += 1
        if start.timestamp() <= mtime < end.timestamp():
            diag["files"] += 1
    return True


def session_records(path: Path, counters: Counter):
    """Yield original line numbers; one damaged file must not stop other sources."""
    try:
        if path.suffix == ".zstd":
            proc = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, text=True)
            if proc.returncode != 0:
                counters["read_errors"] += 1
                return
            stream = proc.stdout.splitlines()
        else:
            stream = path.open(encoding="utf-8", errors="replace")
        try:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("record must be an object")
                except (ValueError, TypeError):
                    counters["invalid_records"] += 1
                    continue
                yield line_no, row
        finally:
            if hasattr(stream, "close"):
                stream.close()
    except (OSError, UnicodeError):
        counters["read_errors"] += 1


def record_events(row: dict, agent: str) -> list[dict]:
    """Keep claims and tool results separate; neither proves a report-level outcome."""
    events = []

    def add(kind, text, call_id=None):
        if kind == "tool_call" and not isinstance(text.get("name"), str):
            raise ValueError("tool name must be a string")
        if call_id is not None and not isinstance(call_id, (str, int)):
            raise ValueError("tool call ID must be a string or integer")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        if text.strip() or kind == "tool_result":
            events.append({"kind": kind, "text": text, "call_id": call_id})

    if agent == "claude-code":
        msg = row.get("message")
        msg = msg if isinstance(msg, dict) else {}
        content = msg.get("content")
        if row.get("type") == "user":
            txt = user_text(content)
            if not is_injected(txt):
                add("user_request", txt)
        elif row.get("type") == "assistant":
            add("assistant_statement", blocks_text(content))
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                add("tool_call", {"name": block.get("name"), "input": block.get("input")},
                    block.get("id"))
            elif block.get("type") == "tool_result":
                add("tool_result", {"content": block.get("content"),
                                    "is_error": block.get("is_error")}, block.get("tool_use_id"))
    elif agent == "codex":
        pl = row.get("payload")
        pl = pl if isinstance(pl, dict) else {}
        kind = pl.get("type")
        if row.get("type") == "event_msg":
            if kind == "user_message" and not is_injected(str(pl.get("message", ""))):
                add("user_request", pl.get("message", ""))
            elif kind == "agent_message":
                add("assistant_statement", pl.get("message", ""))
        elif row.get("type") == "response_item":
            if kind in ("function_call", "custom_tool_call"):
                add("tool_call", {"name": pl.get("name"),
                                  "input": pl.get("arguments", pl.get("input"))}, pl.get("call_id"))
            elif kind in ("function_call_output", "custom_tool_call_output"):
                add("tool_result", pl.get("output", ""), pl.get("call_id"))
    else:
        data = row.get("data")
        data = data if isinstance(data, dict) else {}
        kind = row.get("type")
        if kind == "user/message":
            txt = user_text(data.get("content"))
            if not is_injected(txt):
                add("user_request", txt)
        elif kind == "assistant/message":
            msg = data.get("message")
            add("assistant_statement", blocks_text(msg.get("content") if isinstance(msg, dict) else ""))
        elif kind == "tool/call":
            add("tool_call", {"name": data.get("name"), "input": data.get("arguments")},
                data.get("call_id", data.get("id")))
        elif kind == "tool/result":
            add("tool_result", data, data.get("call_id", data.get("id")))
    return events


def parse_session_file(path: Path, agent: str, start, end, tz, diag: Counter,
                       fallback: str) -> dict | None:
    local = Counter()
    events, users, concl, files = [], [], [], set()
    tools = Counter()
    first = last = cwd = None
    for line_no, row in session_records(path, local):
        pl = row.get("payload")
        if agent == "codex" and row.get("type") == "session_meta" and isinstance(pl, dict):
            cwd = cwd or pl.get("cwd")
        else:
            cwd = cwd or row.get("cwd")
        ts = (epoch_ms(row.get("time"), tz) or parse_ts(row.get("createdAt"), tz)
              if agent == "dsh" else parse_ts(row.get("timestamp"), tz))
        if ts is None:
            try:
                if record_events(row, agent):
                    local["invalid_records"] += 1
            except (TypeError, AttributeError, ValueError):
                local["invalid_records"] += 1
            continue
        local["timestamps"] += 1
        if not start <= ts < end:
            continue
        local["events"] += 1
        first = min(first, ts) if first else ts
        last = max(last, ts) if last else ts
        try:
            extracted = record_events(row, agent)
        except (TypeError, AttributeError, ValueError):
            local["invalid_records"] += 1
            continue
        for event in extracted:
            event.update(at=ts, source=str(path), line=line_no)
            events.append(event)
            if event["kind"] == "user_request":
                users.append(one_line(event["text"], 220))
            elif event["kind"] == "assistant_statement":
                concl.append(one_line(event["text"], 260))
            elif event["kind"] == "tool_call":
                call = json.loads(event["text"])
                tools[call.get("name") or "?"] += 1
                args = call.get("input")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if isinstance(args, dict):
                    for key in ("file_path", "path", "notebook_path"):
                        if isinstance(args.get(key), str):
                            files.add(args[key])
    diag.update(local)
    unknown = not local["timestamps"] or (local["events"] and not events)
    if local["read_errors"] or local["invalid_records"] or unknown:
        diag["failed_files"] += 1
        if unknown and not local["read_errors"]:
            diag["unrecognized_files"] += 1
    else:
        diag["ok_files"] += 1
    if not events:
        return None
    return {"agent": agent, "project": cwd if isinstance(cwd, str) and cwd else fallback,
            "id": path.parent.name if agent == "dsh" else path.stem,
            "start": first, "end": last, "users": users, "concl": concl[-3:],
            "tools": tools, "files": files, "steps": sum(tools.values()), "events": events,
            "source": str(path)}


def scan_sessions(base: Path, pattern: str, agent: str, start, end, tz,
                  diag: Counter | None) -> list[dict]:
    diag = diag if diag is not None else Counter()
    if not base.is_dir():
        diag["unavailable"] = 1
        return []
    if agent == "dsh" and not shutil.which("zstd"):
        diag["unavailable"] = 1
        print("[note] 未找到 zstd，跳过 DSH 会话（brew install zstd）", file=sys.stderr)
        return []
    out = []
    for path in sorted(base.glob(pattern)):
        if not _scan_candidate(path, start, end, diag):
            continue
        fallback = path.parent.name
        if agent == "claude-code":
            fallback = claude_project_map().get(path.parent.name, fallback)
        elif agent == "codex":
            fallback = path.stem
        elif agent == "dsh":
            fallback = path.parent.parent.name.strip("-").replace("-", "/")
        session = parse_session_file(path, agent, start, end, tz, diag, fallback)
        if session:
            out.append(session)
    return out


def claude_sessions(start, end, tz, diag: Counter | None = None) -> list[dict]:
    return scan_sessions(HOME / ".claude" / "projects", "*/*.jsonl", "claude-code",
                         start, end, tz, diag)


def codex_sessions(start, end, tz, diag: Counter | None = None) -> list[dict]:
    return scan_sessions(HOME / ".codex" / "sessions", "*/*/*/*.jsonl", "codex",
                         start, end, tz, diag)


def dsh_sessions(start, end, tz, diag: Counter | None = None) -> list[dict]:
    return scan_sessions(HOME / ".dsh" / "sessions", "*/session-*/*.jsonl.zstd", "dsh",
                         start, end, tz, diag)


# ---------------- 5. 聚合分析（为主题归纳服务） ----------------
def collapse_duplicates(commits: list[dict]) -> tuple[list[dict], int]:
    """只按相同 patch 折叠 rebase/cherry-pick 副本。"""
    groups: dict[tuple, list[dict]] = {}
    for c in commits:
        key = (c["repo"], c["subject"], tuple(sorted(c["files"])))
        groups.setdefault(key, []).append(c)

    shown, extras = [], 0
    for items in groups.values():
        patch_groups: dict[str, list[dict]] = {}
        if len(items) > 1 and all(c.get("repo_path") for c in items):
            for c in items:
                patch_id = git_patch_id(Path(c["repo_path"]), c["full_sha"])
                # Missing/empty patches are kept distinct; matching title and paths
                # alone are not enough evidence that two commits are copies.
                key = patch_id or f"unique:{c['full_sha']}"
                patch_groups.setdefault(key, []).append(c)
        else:
            # Preserve compatibility for callers constructing commit records without
            # repository metadata; production collection always uses patch-id above.
            patch_groups["legacy"] = items

        for same_patch in patch_groups.values():
            ordered = sorted(same_patch, key=lambda c: c["at"])
            keep = dict(max(ordered, key=lambda c: c["added"] + c["deleted"]))
            keep["copies"] = len(ordered) - 1
            if any(item.get("reverted_by") for item in ordered):
                keep["reverted_by"] = next(item["reverted_by"] for item in ordered
                                           if item.get("reverted_by"))
            extras += keep["copies"]
            shown.append(keep)
    shown.sort(key=lambda c: c["at"])
    return shown, extras


def scope_summary(commits: list[dict], top: int = 8) -> list[tuple[str, int, list[tuple[str, int]]]]:
    """按 conventional-commit 的 scope 聚类，给主题归纳一个起点。"""
    per_scope: dict[str, Counter] = {}
    for c in commits:
        m = COMMIT_RE.match(c["subject"])
        typ = (m.group("type").lower() if m else "other")
        scope = (m.group("scope") if m and m.group("scope") else "(无 scope)").strip()
        per_scope.setdefault(scope, Counter())[typ] += 1
    rows = [(s, sum(cnt.values()), cnt.most_common(6)) for s, cnt in per_scope.items()]
    rows.sort(key=lambda r: -r[1])
    return rows[:top]


def pair_reverts(commits: list[dict]) -> dict[str, str]:
    """优先按 Git revert 正文中的目标 SHA 配对；旧记录只做唯一标题回退匹配。"""
    by_subject: dict[tuple, list[dict]] = {}
    for c in commits:
        by_subject.setdefault((c["repo"], c["subject"]), []).append(c)
    pairs: dict[str, str] = {}
    for c in commits:
        if not c["revert"]:
            continue
        target = c.get("revert_target")
        candidates = [item for item in commits
                      if item["repo"] == c["repo"] and target
                      and item["full_sha"].startswith(target)
                      and item["full_sha"] != c["full_sha"]]
        if len(candidates) != 1:
            candidates = []
        if not candidates and not target and c["subject"].startswith('Revert "'):
            inner = c["subject"][len('Revert "'):].rstrip('"').strip()
            candidates = [item for item in by_subject.get((c["repo"], inner), [])
                          if item["full_sha"] != c["full_sha"]]
            # Without a target SHA, pair only when the title identifies one commit.
            if len(candidates) != 1:
                candidates = []
        if candidates:
            candidate = candidates[0]
            pairs[candidate["full_sha"]] = c["full_sha"]
            candidate["reverted_by"] = c["sha"]
            c["reverts_sha"] = candidate["sha"]
        elif target:
            c["reverts_sha"] = target[:7]
    return pairs


def link_sessions(sessions: list[dict], commits: list[dict]) -> None:
    """会话 ↔ 提交的弱关联：同仓库 + 文件相对路径命中。

    只是线索，不是因果；渲染时会标明"可能相关"。
    """
    by_repo_file: dict[tuple, list[dict]] = {}
    for c in commits:
        for f in c["files"]:
            by_repo_file.setdefault((c["repo"], f), []).append(c)
    for s in sessions:
        root = Path(s["project"])
        repo = root.name
        hits: list[dict] = []
        seen: set[str] = set()
        for fp in s["files"]:
            try:
                rel = str(Path(fp).relative_to(root))
            except ValueError:
                continue
            for c in by_repo_file.get((repo, rel), []):
                if c["full_sha"] not in seen:
                    seen.add(c["full_sha"])
                    hits.append(c)
        s["related"] = hits[:3]


def mark_near_duplicates(sessions: list[dict]) -> None:
    """同开场白的分叉/续聊会话：不合并（信息可能不同），只打标提醒别重复计数。"""
    groups: dict[tuple, list[dict]] = {}
    for s in sessions:
        if not s["users"]:
            continue
        key = (s["agent"], s["project"], s["users"][0][:60])
        groups.setdefault(key, []).append(s)
    for group in groups.values():
        if len(group) > 1:
            for s in group:
                s["dup_n"] = len(group)


# ---------------- 6. 渲染 ----------------
def _rel(paths: set[str], cwd: str, limit: int = 4) -> list[str]:
    out = []
    for p in sorted(paths):
        try:
            p = str(Path(p).relative_to(cwd))
        except (ValueError, TypeError):
            pass
        if len(p) < 90:
            out.append(p)
        if len(out) >= limit:
            break
    return out


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def short_id(session_id: str) -> str:
    """原始会话文件名里带 UUID，取末 8 位够 `find` 定位。
    不取开头：Codex 的 UUIDv7 前缀是时间戳，相近时间开的会话会撞。"""
    match = UUID_RE.search(session_id)
    return (match.group(0) if match else session_id)[-8:]


def _period_word(days: int) -> str:
    return "日报" if days == 1 else "周报" if days == 7 else f"{days} 天汇总"


def coverage_status(diag: dict) -> str:
    if diag.get("disabled"):
        return "未启用"
    if diag.get("unavailable"):
        return "来源不可用（目录或依赖缺失）"
    if diag.get("failed_files"):
        return "部分失败" if diag.get("ok_files") else "读取或解析失败"
    return "正常" if diag.get("sessions") or diag.get("commits") else "所选范围无记录"


def event_excerpt(event: dict, limit: int = 260) -> str:
    locator = f"{event['at']:%m-%d %H:%M:%S} · {event['source']}:{event['line']}"
    call = f" · call {event['call_id']}" if event.get("call_id") else ""
    return f"{one_line(event['text'], limit)} （{locator}{call}）"


def render(start, end, cfg, commits, sessions, truncated, dirty=None,
           scope=None, days=1, extras=0, generated=None, period_word=None,
           diagnostics=None) -> str:
    single_day = start.date() == (end - timedelta(seconds=1)).date()
    span = start.strftime("%Y-%m-%d") if single_day \
        else f"{start:%Y-%m-%d} → {(end - timedelta(seconds=1)):%Y-%m-%d}"
    L = [f"# 工作证据 {span}", ""]
    if generated:
        # 证据包是某一时刻的快照；之后再产生的提交/会话不在里面，重跑要加 --force
        L += [f"_生成于 {generated:%Y-%m-%d %H:%M}（UTC{generated:%z}）_", ""]

    if scope:
        L.append("## 采集范围")
        L.append("- Git：" + (", ".join(scope["repos"]) if scope["git"] and scope["repos"]
                              else "未采集" if not scope["git"] else "已启用，但未发现仓库"))
        L.append("- 会话：" + (", ".join(scope["sessions"]) if scope["sessions"] else "未采集"))
        L.append("- 当前工作区状态：" + ("已扫描（运行时快照）" if scope.get("dirty") else "未采集"))
        L.append("")

    if diagnostics:
        L += ["## 采集诊断", ""]
        for name, diag in diagnostics.items():
            L.append(f"- {name}：{coverage_status(diag)}；扫描 {diag.get('scanned', 0)}，"
                     f"无已知解析错误 {diag.get('ok_files', 0)}，失败/部分解析 {diag.get('failed_files', 0)}，"
                     f"坏记录 {diag.get('invalid_records', 0)}；"
                     f"提交 {diag.get('commits', 0)} / 会话 {diag.get('sessions', 0)}；"
                     f"详细摘要 {diag.get('detailed', 0)}，摘要省略 {diag.get('omitted', 0)}")
        L += ["计数单位：Git 为仓库，会话来源为文件；成功和失败文件分别计数。"
              "摘要省略的会话仍在完整索引及结构化证据中。", ""]

    rows = scope_summary(commits)
    if rows:
        L.append("## 主题候选（按 commit scope 聚合，仅供参考）")
        for sc_name, total, types in rows:
            detail = " / ".join(f"{t} {n}" for t, n in types)
            L.append(f"- `{sc_name}`：{total} 条 — {detail}")
        L.append("")

    head = f"## A. Git 提交（{len(commits)} 条"
    head += f"，已折叠 {extras} 个 rebase 副本）" if extras else "）"
    L.append(head)
    if not commits:
        L.append("_无提交_")
    by_repo: dict[str, list] = {}
    for c in commits:
        by_repo.setdefault(c["repo"], []).append(c)
    for repo, items in by_repo.items():
        L.append(f"### {repo}")
        for c in items:
            flags = []
            if c["revert"]:
                flags.append("⚠️REVERT")
            if c.get("reverted_by"):
                flags.append(f"已被 {c['reverted_by']} 回退")
            if c.get("reverts_sha"):
                flags.append(f"回退 {c['reverts_sha']}")
            if c.get("copies"):
                flags.append(f"另有 {c['copies']} 个副本")
            mark = ("  " + "；".join(flags)) if flags else ""
            day = c["at"][5:10]
            note = ""
            if c.get("authored") and c["authored"][:10] != c["at"][:10]:
                try:
                    gap = abs((datetime.fromisoformat(c["at"]) -
                               datetime.fromisoformat(c["authored"])).days)
                except ValueError:
                    gap = 0
                if gap > 1:
                    note = f"（作者 {c['authored'][5:10]}）"
            L.append(f"- [{day}]{note} {c['subject']}{mark}"
                     f"  (+{c['added']}/-{c['deleted']}, {len(c['files'])} files)")
            L.append(f"  证据：commit `{c['full_sha']}` · {c['at']}"
                     f" · 仓库 `{c.get('repo_path', repo)}`（本地记录，不证明已合并或已验证）")
            if c["files"]:
                L.append("  " + ", ".join(c["files"][:5]))
    L.append("")

    L.append(f"## B. Agent 会话（{len(sessions)} 段" +
             ("，部分详细摘要省略，索引完整" if truncated else "") + "）")
    if not sessions:
        L.append("_无会话记录_")
    if sessions:
        L.append("### 完整会话索引")
        for s in sessions:
            detail = "已展开" if s.get("detailed", True) else "摘要省略，可回查"
            hint = one_line(" / ".join(s["users"][:1] + s["users"][-1:] if len(s["users"]) > 1
                                      else s["users"] or s["concl"][-1:]), 160)
            L.append(f"- [{detail}] {s['agent']} · {Path(s['project']).name} · "
                     f"{s['start']:%m-%d %H:%M}–{s['end']:%m-%d %H:%M} · "
                     f"id `{s['id']}` · {hint}")
            if s.get("source"):
                L.append(f"  来源：`{s['source']}`；完整事件及分片定位见结构化证据。")
        L.append("")
    for s in sorted(sessions, key=lambda x: x["start"]):
        if not s.get("detailed", True):
            continue
        proj = Path(s["project"]).name if s["project"] not in (".", "/") else "?"
        tspan = (f"{s['start']:%H:%M}–{s['end']:%H:%M}"
                 if single_day and s["start"].date() == s["end"].date()
                 else f"{s['start']:%m-%d %H:%M}–{s['end']:%m-%d %H:%M}")
        dup = f" · ↻ 同开场白 {s['dup_n']} 段" if s.get("dup_n") else ""
        L.append(f"### [{s['agent']}] {proj} · {tspan} · {s['steps']} 步 · id {short_id(s['id'])}{dup}")
        if s.get("events"):
            requests = [e for e in s["events"] if e["kind"] == "user_request"]
            if len(requests) > 4:
                L.append(f"- 用户请求省略 {len(requests) - 4} 条，见结构化证据。")
                requests = requests[:2] + requests[-2:]
            for event in requests:
                L.append("- 用户请求：" + event_excerpt(event, 220))
            claims = [e for e in s["events"] if e["kind"] == "assistant_statement"]
            for event in claims[-2:]:
                L.append("- 助手陈述（未独立核验）：" + event_excerpt(event))
            results = [e for e in s["events"] if e["kind"] == "tool_result"]
            calls = {e["call_id"]: e for e in s["events"]
                     if e["kind"] == "tool_call" and e.get("call_id")}
            for event in results[-3:]:
                if event.get("call_id") in calls:
                    L.append("- 对应工具调用：" + event_excerpt(calls[event["call_id"]]))
                L.append("- 工具结果（输出摘录，需核对范围）：" + event_excerpt(event))
            L.append(f"- 共 {len(s['events'])} 条原始事件；此处为摘录，"
                     "中间决策及完整输出见结构化证据。")
        elif s["users"]:
            users = s["users"]
            if len(users) > 4:  # 长会话的后半段常有转向，取头尾而不是只取开头
                users = users[:2] + [f"…（省略 {len(users) - 4} 条）"] + users[-2:]
            L.append("- 用户：" + " / ".join(users))
        if s["concl"] and not s.get("events"):
            for c in s["concl"][-2:]:
                L.append(f"- 助手陈述（未独立核验）：{c}")
        if s["tools"]:
            top = ", ".join(f"{n}×{v}" for n, v in s["tools"].most_common(5))
            line = f"- 动手：{top}"
            rel = _rel(s["files"], s["project"])
            if rel:
                line += "；涉及 " + ", ".join(rel)
            L.append(line)
        if s.get("related"):
            links = "; ".join(f"{c['sha']} {c['subject'][:60]}" for c in s["related"])
            L.append(f"- 可能相关提交：{links}")
        L.append("")

    if dirty:
        stale_days = int(cfg.get("stale_days", 7))
        L.append("## C. 当前未提交改动（运行时快照）")
        L.append(f"最近提交距今 ≥{stale_days} 天只表示仓库长期无提交；改动存在时长未知。")
        for d in dirty:
            if d["age_days"] is None:
                age, mark = "未知", ""
            else:
                age = f"{d['age_days']} 天前"
                mark = "  长期无提交的仓库存在当前改动" if d["age_days"] >= stale_days else ""
            L.append(f"- [{d['repo']}] {d['files']} 个文件改动；最近一次提交 {age}{mark}")
        L.append("")

    period = period_word or _period_word(days)
    L.append("## D. 写稿提示")
    L.append(f"写{period}前先读 REFERENCE.md（模板、分栏依据、线索），归纳流程和硬性约束见 SKILL.md；"
             "只写本证据包及关联结构化证据、原始记录支持的事实。"
             "先检查采集诊断和完整会话索引；助手陈述不能单独证明已验证或已合并。")
    return "\n".join(L)


# ---------------- 7. 流程编排 ----------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="区间结束日 YYYY-MM-DD，也可写 today / yesterday；默认今天")
    ap.add_argument("--days", type=int, default=None,
                    help="包含结束日在内向前取几天（默认 1；最近 7 天用 7）")
    week = ap.add_mutually_exclusive_group()
    week.add_argument("--week", action="store_true", help="本周：本周一至今天（不能和 --date/--days 同用）")
    week.add_argument("--last-week", action="store_true",
                      help="上周：上周一至上周日（不能和 --date/--days 同用）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="配置文件（默认 DAILY_REPORT_CONFIG、用户配置或工作区配置）")
    ap.add_argument("--repos-root", help="覆盖 config 的 repos_root")
    ap.add_argument("--author", help="覆盖 config 的 author_emails，正则，多身份用 'a|b'")
    ap.add_argument("--repo", action="append", default=[], help="额外指定仓库，可重复")
    ap.add_argument("--any-author", action="store_true", help="不过滤作者（团队仓库慎用）")
    ap.add_argument("--no-git", action="store_true")
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


def resolve_period(date_arg: str | None, days: int, tz: timezone, week: str | None = None,
                   today=None) -> tuple[datetime, datetime]:
    """Return a half-open date interval [start, end) in the configured timezone.

    week: "this" = 本周一至今天，"last" = 上周一至上周日；此时忽略 date_arg / days。
    today 仅供测试注入。
    """
    today = today or datetime.now(tz).date()
    if week:
        monday = today - timedelta(days=today.weekday())
        if week == "last":
            start_day, end_day = monday - timedelta(days=7), monday - timedelta(days=1)
        else:
            start_day, end_day = monday, today
        start = datetime.combine(start_day, datetime.min.time(), tz)
        return start, datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tz)
    if date_arg in ("today", "yesterday"):
        end_day = today - timedelta(days=1 if date_arg == "yesterday" else 0)
    elif date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != date_arg:
                raise ValueError
            end_day = parsed.date()
        except ValueError as e:
            raise ValueError(
                f"--date 需要 YYYY-MM-DD 格式（或 today / yesterday），收到 {date_arg!r}") from e
    else:
        end_day = today
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tz)
    start = end - timedelta(days=days)
    return start, end


def resolve_author_regex(args: argparse.Namespace, cfg: dict) -> str | None:
    """Resolve author precedence: CLI, config, then global Git identity."""
    author_re = args.author
    if not author_re:
        emails = cfg.get("author_emails") or [git_config_value("user.email")]
        alternatives = "|".join(re.escape(email) for email in emails if email)
        # Git renders authors as "name <email>"; only --author accepts arbitrary regexes.
        author_re = rf"<(?:{alternatives})>\Z" if alternatives else None
    if author_re:
        try:
            re.compile(author_re, re.I)
        except re.error as e:
            raise ValueError(f"--author / author_emails 不是合法正则：{e}") from e
    return author_re


def git_config_value(key: str) -> str:
    if not shutil.which("git"):
        return ""
    result = subprocess.run(["git", "config", "--global", key],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def collect_commits(repos: list[Path], start: datetime, end: datetime,
                    author_re: str | None, any_author: bool,
                    my_name: str, diag: Counter | None = None) -> tuple[list[dict], dict[str, int]]:
    commits, seen = [], set()
    alias_hits: dict[str, int] = {}
    for repo in repos:
        for commit in git_commits(repo, start, end, diag):
            mine = bool(author_re and re.search(author_re, commit["author"], re.I))
            if not mine and not any_author:
                if my_name and my_name.lower() in commit["author"].lower():
                    alias_hits[commit["author"]] = alias_hits.get(commit["author"], 0) + 1
                continue
            if commit["full_sha"] not in seen:
                seen.add(commit["full_sha"])
                commits.append(commit)
    commits.sort(key=lambda commit: commit["at"])
    return commits, alias_hits


SESSION_SOURCES = (
    ("claude", claude_sessions, "~/.claude/projects"),
    ("codex", codex_sessions, "~/.codex/sessions"),
    ("dsh", dsh_sessions, "~/.dsh/sessions"),
)


def warn_format_drift(name: str, location: str, diag: Counter, found: int) -> None:
    """会话格式变了会让解析静默返回 0 段；用「文件被改过但没事件 / 有事件但没内容」识别出来。"""
    if diag["files"] and not diag["events"]:
        print(f"[warn] {name}: 窗口内有 {diag['files']} 个会话文件被修改，但没解析出任何事件；"
              f"会话格式可能变了，检查 {location}。这不代表当天没有会话", file=sys.stderr)
    elif diag["events"] and not found:
        print(f"[warn] {name}: 解析到 {diag['events']} 条事件，但没提取出任何用户消息或工具调用；"
              f"会话格式可能变了，检查 {location}", file=sys.stderr)


def select_session_details(sessions: list[dict], limit: int) -> None:
    """Round-robin dates, then projects; tool count does not determine importance."""
    dates = {}
    for session in sessions:
        session["detailed"] = False
        projects = dates.setdefault(session["start"].date(), {})
        projects.setdefault(session["project"], []).append(session)
    queues = []
    for projects in dates.values():
        queue = []
        groups = list(projects.values())
        while any(groups):
            for group in groups:
                if group:
                    queue.append(group.pop(0))
        queues.append(queue)
    while limit and any(queues):
        for queue in queues:
            if queue and limit:
                queue.pop(0)["detailed"] = True
                limit -= 1


def collect_sessions(start: datetime, end: datetime, tz: timezone, sources: dict,
                     disabled: bool, commits: list[dict], limit: int,
                     diagnostics: dict | None = None) -> tuple[list[dict], bool]:
    diagnostics = diagnostics if diagnostics is not None else {}
    sessions = []
    for name, parse, location in SESSION_SOURCES:
        diag: Counter = Counter()
        diagnostics[name] = diag
        if disabled or not sources.get(name, True):
            diag["disabled"] = 1
            continue
        found = parse(start, end, tz, diag)
        diag["sessions"] = len(found)
        warn_format_drift(name, location, diag, len(found))
        if diag["failed_files"]:
            print(f"[warn] {name}: {diag['failed_files']} 个文件读取或解析不完整；"
                  "详见证据包采集诊断", file=sys.stderr)
        sessions.extend(found)

    # A DSH session may be split across files. Keep all fragments and their locators.
    by_id: dict[tuple, dict] = {}
    for session in sessions:
        key = (session["agent"], session["id"])
        if key not in by_id:
            by_id[key] = session
            continue
        kept = by_id[key]
        kept["start"] = min(kept["start"], session["start"])
        kept["end"] = max(kept["end"], session["end"])
        kept["events"] = sorted(kept.get("events", []) + session.get("events", []),
                                key=lambda event: (event["at"], event["source"], event["line"]))
        kept["users"] = [one_line(event["text"], 220) for event in kept["events"]
                         if event["kind"] == "user_request"]
        kept["concl"] = [one_line(event["text"], 260) for event in kept["events"]
                         if event["kind"] == "assistant_statement"][-3:]
        kept["tools"].update(session["tools"])
        kept["files"].update(session["files"])
        kept["steps"] += session["steps"]
    sessions = sorted(by_id.values(), key=lambda session: (session["start"], session["id"]))
    mark_near_duplicates(sessions)
    link_sessions(sessions, commits)
    select_session_details(sessions, limit)
    agent_names = {"claude-code": "claude", "codex": "codex", "dsh": "dsh"}
    for name in agent_names.values():
        diagnostics[name]["sessions"] = 0
    for session in sessions:
        diag = diagnostics[agent_names[session["agent"]]]
        diag["sessions"] += 1
        diag["detailed" if session["detailed"] else "omitted"] += 1
    return sessions, len(sessions) > limit


def print_identity_report(repos: list[Path], repos_root: Path, cfg: dict,
                          author_re: str | None, user_name: str, user_email: str) -> None:
    print(f"git user.name  : {user_name}")
    print(f"git user.email : {user_email}")
    print(f"author_emails  : {cfg.get('author_emails') or '(空，回退到 git user.email)'}")
    print(f"匹配正则       : {author_re}")
    print(f"仓库根         : {repos_root}  ({len(repos)} 个仓库)\n")
    for repo in repos:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--exclude=refs/stash", "--all",
             "--since=90 days ago", "--pretty=%an <%ae>"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[warn] 无法读取仓库 {repo}: {(result.stderr or '').strip()}", file=sys.stderr)
            continue
        counts = Counter(result.stdout.splitlines())
        if not counts:
            continue
        print(f"[{repo.name}]")
        for who, count in counts.most_common(4):
            mark = "  <- 匹配" if author_re and re.search(author_re, who, re.I) else ""
            print(f"  {count:4d}  {who}{mark}")


def collect_dirty_state(repos: list[Path], configured: bool, explicit: bool,
                        start: datetime, end: datetime,
                        now: datetime) -> tuple[list[dict], bool]:
    period_contains_now = start <= now < end
    enabled = explicit or (configured and period_contains_now)
    if not enabled:
        if configured:
            print("[note] 历史/未来日期范围忽略 config.dirty；当前工作区状态只在范围包含今天时自动采集",
                  file=sys.stderr)
        return [], False
    if explicit and not period_contains_now:
        print("[warn] --dirty 采集的是运行时工作区状态，不代表所选历史/未来日期的状态",
              file=sys.stderr)
    states = [state for repo in repos if (state := dirty_state(repo, now))]
    states.sort(key=lambda item: (item["age_days"] is None, -(item["age_days"] or 0)))
    return states, bool(repos)


def evidence_path(cfg: dict, args, start: datetime, days: int) -> tuple[Path, Path]:
    default_dir = Path(cfg["reports_dir"]).expanduser() / ".evidence"
    if args.out:
        return default_dir, Path(args.out).expanduser()
    name = f"{start:%Y%m%d}" + (f"-{days}d" if days > 1 else "") + ".md"
    return default_dir, default_dir / name


def prune_evidence(directory: Path, keep: int) -> None:
    """只清理默认证据目录，避免误删 --out 指向的文件。"""
    try:
        files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        print(f"[warn] 无法整理证据目录 {directory}: {e}", file=sys.stderr)
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError as e:
            print(f"[warn] 无法删除旧证据包 {old}: {e}", file=sys.stderr)


def json_evidence(value, redact: bool):
    """Redact string values before encoding, so replacements cannot corrupt JSON."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_text(value) if redact else value
    if isinstance(value, dict):
        return {key: json_evidence(item, redact) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = sorted(value) if isinstance(value, set) else value
        return [json_evidence(item, redact) for item in items]
    return value


def evidence_counts(text: str) -> dict[str, int]:
    counts = {}
    for key, pattern in (("commits", r"## A\. Git 提交（(\d+) 条"),
                         ("sessions", r"## B\. Agent 会话（(\d+) 段")):
        match = re.search(pattern, text)
        if match:
            counts[key] = int(match.group(1))
    return counts


def write_evidence(path: Path, text: str, force: bool, evidence=None) -> str | None:
    """Save immutable evidence first, then atomically replace the latest Markdown view."""
    if path.exists() and not force:
        return f"证据包已存在：{path}；确认要覆盖时加 --force"
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        history = path.parent / ".history" / path.name
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-")
        snapshot = Path(tempfile.mkdtemp(prefix=stamp, dir=history)).resolve()
        if previous is not None:
            # Also preserve legacy Markdown that predates versioned snapshots.
            (snapshot / "previous.md").write_text(previous, encoding="utf-8")
            old_counts, new_counts = evidence_counts(previous), evidence_counts(text)
            decreased = [f"{key} {old_counts[key]} → {new_counts[key]}"
                         for key in old_counts.keys() & new_counts.keys()
                         if new_counts[key] < old_counts[key]]
            if decreased:
                warning = "重采证据数量减少：" + "，".join(sorted(decreased))
                text += f"\n\n## 快照差异\n{warning}。旧证据保存在 `{snapshot / 'previous.md'}`；" \
                        "可能是采集范围变化或原始记录缺失，请核对；未自动合并新旧证据。\n"
                print(f"[warn] {warning}；旧快照已保留", file=sys.stderr)
        if evidence is not None:
            structured = snapshot / "evidence.json"
            structured.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
            text += f"\n\n结构化证据（完整会话事件与定位）：`{structured}`\n"
        text += f"\n本次快照：`{snapshot / 'evidence.md'}`\n"
        (snapshot / "evidence.md").write_text(text, encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                        prefix=f".{path.name}.", suffix=".tmp",
                                        delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
        os.replace(temporary_path, path)
    except (OSError, UnicodeError) as e:
        if temporary_path:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return f"无法写入证据包 {path}：{e}"
    return None


def main() -> int:
    ap = build_parser()
    a = ap.parse_args()

    if a.days is not None and a.days < 1:
        ap.error("--days 必须大于等于 1")
    if (a.week or a.last_week) and (a.date or a.days is not None):
        ap.error("--week / --last-week 不能和 --date / --days 同时使用")

    config_path = Path(a.config).expanduser()
    if not config_path.exists():
        print(f"[warn] 配置文件不存在，使用默认值：{config_path}", file=sys.stderr)
    try:
        cfg = load_config(config_path)
    except ValueError as e:
        ap.error(str(e))
    config_errors = validate_config(cfg)
    if config_errors:
        ap.error("配置无效：" + "；".join(config_errors))
    limit = a.max_sessions if a.max_sessions is not None else cfg["max_sessions"]
    if limit < 1:
        ap.error("--max-sessions / config.max_sessions 必须大于等于 1")
    if a.keep is not None and a.keep < 0:
        ap.error("--keep 必须大于等于 0")

    try:
        offset = float(cfg.get("utc_offset_hours", 8))
    except (TypeError, ValueError):
        ap.error(f"config.utc_offset_hours 必须是数字，收到 {cfg.get('utc_offset_hours')!r}")
    if not -12 <= offset <= 14:
        ap.error(f"config.utc_offset_hours 超出合理范围（-12..14）：{offset}")
    tz = timezone(timedelta(hours=offset))

    try:
        start, end = resolve_period(a.date, 1 if a.days is None else a.days, tz,
                                    week="this" if a.week else "last" if a.last_week else None)
        author_re = resolve_author_regex(a, cfg)
    except ValueError as e:
        ap.error(str(e))

    days = (end - start).days
    repos_root = Path(a.repos_root or cfg["repos_root"])
    src = cfg.get("sources", {})
    git_enabled = not a.no_git and src.get("git", True)
    if git_enabled and not shutil.which("git"):
        print("[error] 找不到 git 命令，请确认已安装并在 PATH 中", file=sys.stderr)
        return 2

    repos = [Path(p).expanduser() for p in a.repo] if git_enabled else []
    if git_enabled:
        repos += [p for p in discover_repos(repos_root) if p not in repos]

    my_name = git_config_value("user.name")
    my_email = git_config_value("user.email")

    if a.whoami:
        print_identity_report(repos, repos_root, cfg, author_re, my_name, my_email)
        return 0

    diagnostics = {"git": Counter(scanned=len(repos), disabled=int(not git_enabled),
                                  unavailable=int(git_enabled and not repos))}
    commits, alias_hits = collect_commits(repos, start, end, author_re, a.any_author, my_name,
                                         diagnostics["git"])
    pair_reverts(commits)
    shown, extras = collapse_duplicates(commits)
    diagnostics["git"]["commits"] = len(shown)
    if extras:
        print(f"[note] 折叠了 {extras} 个 patch-id 相同的 rebase/cherry-pick 副本，"
              f"写报告时按同一事项处理", file=sys.stderr)

    if alias_hits:
        detail = ", ".join(f"{k} ({v} 个提交)" for k, v in
                           sorted(alias_hits.items(), key=lambda x: -x[1]))
        print(f"[warn] 有和 {my_name} 同名、但邮箱没配的提交：{detail}\n"
              f"       如果这些是你（换过邮箱），把它们加进 config.author_emails 后重跑；"
              f"如果是重名同事，忽略即可", file=sys.stderr)

    sessions, truncated = collect_sessions(start, end, tz, src, a.no_sessions, shown, limit,
                                          diagnostics)

    now = datetime.now(tz)
    dirty, dirty_scanned = collect_dirty_state(
        repos, bool(cfg.get("dirty")), a.dirty, start, end, now)

    session_sources = [name for name in ("claude", "codex", "dsh")
                       if not a.no_sessions and src.get(name, True)]
    scope = {
        "git": git_enabled,
        "repos": sorted({p.name for p in repos}) if git_enabled else [],
        "sessions": session_sources,
        "dirty": dirty_scanned,
    }
    text = render(start, end, cfg, shown, sessions, truncated, dirty, scope,
                  days=days, extras=extras, generated=now,
                  period_word="周报" if a.week or a.last_week else None,
                  diagnostics=diagnostics)

    do_redact = (not a.no_redact) and bool(cfg.get("redact", True))
    if do_redact:
        text = redact_text(text)

    if a.to_stdout:
        print(text)
        print(f"[ok] {len(shown)} commits, {len(sessions)} sessions", file=sys.stderr)
        return 0

    evidence_dir, out = evidence_path(cfg, a, start, days)
    evidence = json_evidence({
        "schema_version": 1, "generated_at": now, "start": start, "end_exclusive": end,
        "scope": scope, "diagnostics": diagnostics, "commits": shown,
        "sessions": sessions, "dirty": dirty,
    }, do_redact)
    write_error = write_evidence(out, text, a.force, evidence)
    if write_error:
        print(f"[error] {write_error}", file=sys.stderr)
        return 2
    print(f"[ok] {len(shown)} commits, {len(sessions)} sessions -> {out}", file=sys.stderr)

    keep = a.keep if a.keep is not None else int(cfg.get("evidence_keep", 30))
    if keep > 0 and out.parent == evidence_dir:
        prune_evidence(evidence_dir, keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
