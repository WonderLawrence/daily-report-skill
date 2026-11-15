"""Evidence redaction, serialization and immutable snapshot storage.

This module depends on no collector configuration and no data source: it only answers
"what happens before evidence hits the disk".
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


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
    """Last line of defense before writing evidence.

    Not exhaustive: it blocks the most common high risk shapes, nothing more.
    """
    for pat, repl in REDACT_RULES:
        text = pat.sub(repl, text)
    return text


def prune_evidence(directory: Path, keep: int) -> None:
    """Prune only the default evidence directory, never a file pointed at by --out."""
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
    """Redact string values before encoding, so replacements cannot corrupt JSON.

    Dataclasses expand field by field in declaration order, so the field order in models.py
    is the key order in evidence.json.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: json_evidence(getattr(value, f.name), redact)
                for f in dataclasses.fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_text(value) if redact else value
    if isinstance(value, dict):
        return {key: json_evidence(item, redact) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = sorted(value) if isinstance(value, set) else value
        return [json_evidence(item, redact) for item in items]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Falling through used to hand json.dumps something it cannot encode, which surfaced as
    # a traceback from deep inside write_evidence. Name the type here instead.
    raise TypeError(f"证据无法序列化的类型：{type(value).__name__}")


# Machine readable marker written by the renderer. Never parse the prose: section titles
# are wording, and a reworded title would silently stop the re-collection guard.
COUNTS_MARKER = "<!-- evidence-counts commits={commits} sessions={sessions} -->"
COUNTS_RE = re.compile(r"<!-- evidence-counts ((?:\w+=\d+ ?)+)-->")


def format_counts(commits: int, sessions: int) -> str:
    return COUNTS_MARKER.format(commits=commits, sessions=sessions)


def evidence_counts(text: str) -> dict[str, int]:
    """Read the marker a previous run wrote; an unmarked file simply yields nothing."""
    match = COUNTS_RE.search(text)
    if not match:
        return {}
    return {key: int(value) for key, value in
            (pair.split("=") for pair in match.group(1).split())}


def write_evidence(path: Path, text: str, force: bool, evidence=None,
                   redact_previous: bool = False) -> str | None:
    """Save immutable evidence first, then atomically replace the latest Markdown view."""
    if path.exists() and not force:
        return f"证据包已存在：{path}；确认要覆盖时加 --force"
    try:
        # Encode before touching the filesystem: a serialization failure must not leave a
        # half built snapshot directory behind.
        structured = (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
                      if evidence is not None else None)
    except (TypeError, ValueError) as e:
        return f"无法序列化结构化证据：{e}"
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
            archived = redact_text(previous) if redact_previous else previous
            (snapshot / "previous.md").write_text(archived, encoding="utf-8")
            old_counts, new_counts = evidence_counts(previous), evidence_counts(text)
            decreased = [f"{key} {old_counts[key]} → {new_counts[key]}"
                         for key in old_counts.keys() & new_counts.keys()
                         if new_counts[key] < old_counts[key]]
            if decreased:
                warning = "重采证据数量减少：" + "，".join(sorted(decreased))
                text += f"\n\n## 快照差异\n{warning}。旧证据保存在 `{snapshot / 'previous.md'}`；" \
                        "可能是采集范围变化或原始记录缺失，请核对；未自动合并新旧证据。\n"
                print(f"[warn] {warning}；旧快照已保留", file=sys.stderr)
        if structured is not None:
            structured_path = snapshot / "evidence.json"
            structured_path.write_text(structured, encoding="utf-8")
            text += f"\n\n结构化证据（完整会话事件与定位）：`{structured_path}`\n"
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
