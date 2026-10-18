#!/usr/bin/env python3
"""collect.py — 采集一天（或 N 天）的「Git 提交 + Agent 会话」证据，输出喂给 LLM 写日报的 Markdown。

只依赖标准库；DSH 会话是 zstd 压缩，走 `zstd -dc`，没装就自动跳过。

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
    "max_sessions": 30,           # 超出截断，避免 prompt 爆炸
    "stale_days": 7,
    "redact": True,               # 证据落盘前脱敏（token/私钥/内网 IP）
    "evidence_keep": 30,          # 保留最近 N 份证据包，0 = 不清理
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
    except (TypeError, ValueError, OSError):
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


def git_commits(repo: Path, start, end) -> list[dict]:
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
        print(f"[warn] 无法读取仓库 {repo}: {(result.stderr or '').strip()}", file=sys.stderr)
        return []
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
    patch = subprocess.run(["git", "patch-id", "--stable"], input=show.stdout,
                           capture_output=True, text=True)
    if patch.returncode != 0 or not patch.stdout.strip():
        return None
    return patch.stdout.split()[0]


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
                    if d.get("cwd"):
                        _PROJECT_MAP[f.parent.name] = d["cwd"]
                        break
        except OSError:
            continue
    return _PROJECT_MAP


def claude_sessions(start, end, tz) -> list[dict]:
    base = HOME / ".claude" / "projects"
    if not base.is_dir():
        return []
    pmap = claude_project_map()
    out = []
    for f in base.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < start.timestamp() - 86400:
                continue
        except OSError:
            continue
        users, concl, tools, files = [], [], Counter(), set()
        first = last = cwd = None
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = parse_ts(d.get("timestamp"), tz)
                if ts is None or not (start <= ts < end):
                    continue
                first = first or ts
                last = ts
                cwd = cwd or d.get("cwd")
                kind = d.get("type")
                msg = d.get("message") or {}
                if kind == "user":
                    txt = blocks_text(msg.get("content")).strip()
                    if not is_injected(txt):
                        users.append(one_line(txt, 220))
                elif kind == "assistant":
                    txt = blocks_text(msg.get("content")).strip()
                    if txt:
                        concl.append(one_line(txt, 260))
                    for b in msg.get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tools[b.get("name", "?")] += 1
                            inp = b.get("input") or {}
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp:
                                files.add(str(fp))
        if first and (users or tools):
            out.append({"agent": "claude-code",
                        "project": cwd or pmap.get(f.parent.name) or f.parent.name,
                        "id": f.stem, "start": first, "end": last or first,
                        "users": users, "concl": concl[-3:], "tools": tools,
                        "files": files, "steps": sum(tools.values())})
    return out


# ---------------- 3. Codex CLI ----------------
def codex_sessions(start, end, tz) -> list[dict]:
    base = HOME / ".codex" / "sessions"
    if not base.is_dir():
        return []
    files_on_disk: list[Path] = []
    day = start.date()
    while day <= end.date():
        files_on_disk += list(base.glob(f"{day:%Y/%m/%d}/*.jsonl"))
        day += timedelta(days=1)
    out = []
    for f in files_on_disk:
        users, concl, tools = [], [], Counter()
        first = last = cwd = None
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts = parse_ts(d.get("timestamp"), tz)
                if ts is None or not (start <= ts < end):
                    continue
                first = first or ts
                last = ts
                pl = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    cwd = cwd or pl.get("cwd")
                elif d.get("type") == "event_msg":
                    if pl.get("type") == "user_message":
                        txt = str(pl.get("message", ""))
                        if not is_injected(txt):
                            users.append(one_line(txt, 220))
                    elif pl.get("type") == "agent_message":
                        concl.append(one_line(pl.get("message", ""), 260))
                elif d.get("type") == "response_item":
                    if pl.get("type") == "function_call":
                        tools[pl.get("name", "?")] += 1
        if first and (users or tools):
            out.append({"agent": "codex", "project": cwd or f.stem, "id": f.stem,
                        "start": first, "end": last or first, "users": users,
                        "concl": concl[-3:], "tools": tools, "files": set(),
                        "steps": sum(tools.values())})
    return out


# ---------------- 4. DSH（zstd 压缩的 v4 jsonl） ----------------
def dsh_sessions(start, end, tz) -> list[dict]:
    base = HOME / ".dsh" / "sessions"
    if not base.is_dir() or not shutil.which("zstd"):
        if base.is_dir() and not shutil.which("zstd"):
            print("[note] 未找到 zstd，跳过 DSH 会话（brew install zstd）", file=sys.stderr)
        return []
    out = []
    for f in base.glob("*/session-*/*.jsonl.zstd"):
        try:
            if f.stat().st_mtime < start.timestamp() - 86400:
                continue
        except OSError:
            continue
        proc = subprocess.run(["zstd", "-dc", str(f)], capture_output=True, text=True)
        if proc.returncode != 0:
            continue
        users, concl, tools, files = [], [], Counter(), set()
        first = last = cwd = None
        for line in proc.stdout.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            data = d.get("data") or {}
            if t == "session":
                cwd = d.get("cwd") or cwd
                continue
            ts = epoch_ms(d.get("time"), tz) or parse_ts(d.get("createdAt"), tz)
            if ts is None or not (start <= ts < end):
                continue
            first = first or ts
            last = ts
            if t == "user/message":
                txt = blocks_text(data.get("content")).strip()
                if not is_injected(txt):
                    users.append(one_line(txt, 220))
            elif t == "assistant/message":
                txt = blocks_text((data.get("message") or {}).get("content")).strip()
                if txt:
                    concl.append(one_line(txt, 260))
            elif t == "tool/call":
                tools[data.get("name", "?")] += 1
                try:
                    args = json.loads(data.get("arguments") or "{}")
                except Exception:
                    args = {}
                for k in ("file_path", "path", "notebook_path"):
                    if args.get(k):
                        files.add(str(args[k]))
        if first and (users or tools):
            out.append({"agent": "dsh",
                        "project": cwd or f.parent.parent.name.strip("-").replace("-", "/"),
                        "id": f.parent.name, "start": first, "end": last or first,
                        "users": users, "concl": concl[-3:], "tools": tools,
                        "files": files, "steps": sum(tools.values())})
    return out


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


def _period_word(days: int) -> str:
    return "日报" if days == 1 else "周报" if days == 7 else f"{days} 天汇总"


def render(start, end, cfg, commits, sessions, truncated, dirty=None,
           scope=None, days=1, extras=0) -> str:
    span = start.strftime("%Y-%m-%d") if start.date() == (end - timedelta(seconds=1)).date() \
        else f"{start:%Y-%m-%d} → {(end - timedelta(seconds=1)):%Y-%m-%d}"
    L = [f"# 工作证据 {span}", ""]

    if scope:
        L.append("## 采集范围")
        L.append("- Git：" + (", ".join(scope["repos"]) if scope["git"] and scope["repos"]
                              else "未采集" if not scope["git"] else "已启用，但未发现仓库"))
        L.append("- 会话：" + (", ".join(scope["sessions"]) if scope["sessions"] else "未采集"))
        L.append("- 当前工作区状态：" + ("已扫描（运行时快照）" if scope.get("dirty") else "未采集"))
        L.append("")

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
            if c["files"]:
                L.append("  " + ", ".join(c["files"][:5]))
    L.append("")

    L.append(f"## B. Agent 会话（{len(sessions)} 段" +
             ("，已截断" if truncated else "") + "）")
    if not sessions:
        L.append("_无会话记录_")
    for s in sorted(sessions, key=lambda x: x["start"]):
        proj = Path(s["project"]).name if s["project"] not in (".", "/") else "?"
        tspan = (f"{s['start']:%H:%M}–{s['end']:%H:%M}"
                 if s["start"].date() == s["end"].date()
                 else f"{s['start']:%m-%d %H:%M}–{s['end']:%m-%d %H:%M}")
        dup = f" · ↻ 同开场白 {s['dup_n']} 段" if s.get("dup_n") else ""
        L.append(f"### [{s['agent']}] {proj} · {tspan} · {s['steps']} 步{dup}")
        if s["users"]:
            L.append("- 用户：" + " / ".join(s["users"][:4]))
        if s["concl"]:
            for c in s["concl"][-2:]:
                L.append(f"- 结论：{c}")
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
        L.append(f"## C. 未提交改动（≥{stale_days} 天未提交的归「遗留变更」）")
        for d in dirty:
            if d["age_days"] is None:
                age, mark = "未知", ""
            else:
                age = f"{d['age_days']} 天前"
                mark = "  ⚠️遗留" if d["age_days"] >= stale_days else ""
            L.append(f"- [{d['repo']}] {d['files']} 个文件改动；最近一次提交 {age}{mark}")
        L.append("")

    period = _period_word(days)
    L.append("## D. 写稿提示")
    L.append(f"按 REFERENCE.md 的「结构选择」和「归纳流程」写{period}：")
    L.append("1. 先建立主题簇——同一目标下的功能、修复、重构、回退合并成一个主题，再起标题；"
             "不要按 commit 前缀分栏。")
    L.append("2. 只写本文件出现过的事实；动机必须有会话或提交证据。")
    L.append("3. 已提交的 revert 在主题内交代「为什么回退、最终保留什么」；仅在会话中被否决的方案"
             "同样写进主题，不能包装成成果。")
    L.append("4. 状态分开写：已合并 / 仅本地 / 已回退 / 待决策。")
    L.append("5. A 为空但 B 有内容时，不要写「没有产出」。")
    L.append("6. `可能相关提交` 只是文件路径交集给出的线索，不是因果，确认不了就别写。")
    return "\n".join(L)


# ---------------- 7. 流程编排 ----------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="区间结束日 YYYY-MM-DD，默认今天")
    ap.add_argument("--days", type=int, default=1, help="包含结束日在内向前取几天（周报用 7）")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help="配置文件（默认 DAILY_REPORT_CONFIG、用户配置或工作区配置）")
    ap.add_argument("--repos-root", help="覆盖 config 的 repos_root")
    ap.add_argument("--author", help="覆盖 config 的 author_emails，正则，多身份用 'a|b'")
    ap.add_argument("--repo", action="append", default=[], help="额外指定仓库，可重复")
    ap.add_argument("--any-author", action="store_true", help="不过滤作者（团队仓库慎用）")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-sessions", action="store_true")
    ap.add_argument("--dirty", action="store_true",
                    help="附带未提交改动（tracked 文件），用于「遗留变更」章节")
    ap.add_argument("--no-redact", action="store_true", help="关闭证据包脱敏")
    ap.add_argument("--keep", type=int, help="证据包保留份数，0 = 不清理（默认取 config）")
    ap.add_argument("--max-sessions", type=int)
    ap.add_argument("--whoami", action="store_true",
                    help="只做身份体检：打印 git 身份、配置里的邮箱、各仓库近期作者")
    ap.add_argument("--out", help="证据包输出路径")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的证据包")
    ap.add_argument("--print", dest="to_stdout", action="store_true", help="打到 stdout")
    return ap


def resolve_period(date_arg: str | None, days: int, tz: timezone) -> tuple[datetime, datetime]:
    """Return a half-open date interval [start, end) in the configured timezone."""
    if date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != date_arg:
                raise ValueError
            end_day = parsed.date()
        except ValueError as e:
            raise ValueError(f"--date 需要 YYYY-MM-DD 格式，收到 {date_arg!r}") from e
    else:
        end_day = datetime.now(tz).date()
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tz)
    start = end - timedelta(days=days)
    return start, end


def resolve_author_regex(args: argparse.Namespace, cfg: dict) -> str | None:
    """Resolve author precedence: CLI, config, then global Git identity."""
    author_re = args.author
    if not author_re and cfg.get("author_emails"):
        author_re = "|".join(re.escape(email) for email in cfg["author_emails"])
    if not author_re:
        author_re = git_config_value("user.email") or None
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
                    my_name: str) -> tuple[list[dict], dict[str, int]]:
    commits, seen = [], set()
    alias_hits: dict[str, int] = {}
    for repo in repos:
        for commit in git_commits(repo, start, end):
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


def collect_sessions(start: datetime, end: datetime, tz: timezone, sources: dict,
                     disabled: bool, commits: list[dict], limit: int) -> tuple[list[dict], bool]:
    sessions = []
    if not disabled:
        if sources.get("claude", True):
            sessions.extend(claude_sessions(start, end, tz))
        if sources.get("codex", True):
            sessions.extend(codex_sessions(start, end, tz))
        if sources.get("dsh", True):
            sessions.extend(dsh_sessions(start, end, tz))

    by_id: dict[tuple, dict] = {}
    for session in sessions:
        key = (session["agent"], session["id"])
        if key not in by_id or session["steps"] > by_id[key]["steps"]:
            by_id[key] = session
    sessions = sorted(by_id.values(), key=lambda session: session["start"])
    mark_near_duplicates(sessions)
    link_sessions(sessions, commits)

    truncated = len(sessions) > limit
    if truncated:
        sessions = sorted(sorted(sessions, key=lambda session: session["steps"], reverse=True)[:limit],
                          key=lambda session: session["start"])
    return sessions, truncated


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


def evidence_path(cfg: dict, args, start: datetime) -> tuple[Path, Path]:
    default_dir = Path(cfg["reports_dir"]).expanduser() / ".evidence"
    if args.out:
        return default_dir, Path(args.out).expanduser()
    name = f"{start:%Y%m%d}" + (f"-{args.days}d" if args.days > 1 else "") + ".md"
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


def write_evidence(path: Path, text: str, force: bool) -> str | None:
    """Write evidence atomically; return an error message on failure."""
    if path.exists() and not force:
        return f"证据包已存在：{path}；确认要覆盖时加 --force"
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                        prefix=f".{path.name}.", suffix=".tmp",
                                        delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
        os.replace(temporary_path, path)
    except OSError as e:
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

    if a.days < 1:
        ap.error("--days 必须大于等于 1")

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
        start, end = resolve_period(a.date, a.days, tz)
        author_re = resolve_author_regex(a, cfg)
    except ValueError as e:
        ap.error(str(e))

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

    commits, alias_hits = collect_commits(repos, start, end, author_re, a.any_author, my_name)
    pair_reverts(commits)
    shown, extras = collapse_duplicates(commits)
    if extras:
        print(f"[note] 折叠了 {extras} 个 patch-id 相同的 rebase/cherry-pick 副本，"
              f"写报告时按同一事项处理", file=sys.stderr)

    if alias_hits:
        detail = ", ".join(f"{k} ({v} 个提交)" for k, v in
                           sorted(alias_hits.items(), key=lambda x: -x[1]))
        print(f"[warn] 有和 {my_name} 同名、但邮箱没配的提交：{detail}\n"
              f"       如果这些是你（换过邮箱），把它们加进 config.author_emails 后重跑；"
              f"如果是重名同事，忽略即可", file=sys.stderr)

    sessions, truncated = collect_sessions(start, end, tz, src, a.no_sessions, shown, limit)

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
                  days=a.days, extras=extras)

    do_redact = (not a.no_redact) and bool(cfg.get("redact", True))
    if do_redact:
        text = redact_text(text)

    if a.to_stdout:
        print(text)
        print(f"[ok] {len(shown)} commits, {len(sessions)} sessions", file=sys.stderr)
        return 0

    evidence_dir, out = evidence_path(cfg, a, start)
    write_error = write_evidence(out, text, a.force)
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
