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

from . import messages as m


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
        print(m.EV_WARN_PRUNE_DIR.format(directory=directory, error=e), file=sys.stderr)
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError as e:
            print(m.EV_WARN_PRUNE_FILE.format(path=old, error=e), file=sys.stderr)


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
    raise TypeError(m.EV_ERR_UNSERIALIZABLE_TYPE.format(type=type(value).__name__))


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


class EvidenceWriteError(Exception):
    """Saving failed; no success receipt may be emitted."""


@dataclasses.dataclass(frozen=True)
class WrittenEvidence:
    markdown_path: Path
    snapshot_markdown_path: Path
    evidence_path: Path | None

    def receipt(self) -> dict:
        """Small machine-readable result; paths refer to this exact immutable snapshot."""
        return {"kind": "evidence_receipt", "schema_version": 1,
                **json_evidence(self, False)}


def write_evidence(path: Path, text: str, overwrite: bool, evidence=None,
                   redact_previous: bool = False) -> WrittenEvidence:
    """Save immutable evidence first, then atomically replace the latest Markdown view."""
    if path.exists() and not overwrite:
        raise EvidenceWriteError(m.EV_ERR_EXISTS.format(path=path))
    try:
        # Encode before touching the filesystem: a serialization failure must not leave a
        # half built snapshot directory behind.
        structured = (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
                      if evidence is not None else None)
    except (TypeError, ValueError) as e:
        raise EvidenceWriteError(m.EV_ERR_SERIALIZE.format(detail=e)) from e
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
                warning = m.EV_COUNTS_DECREASED.format(details="，".join(sorted(decreased)))
                body = m.EV_SNAPSHOT_DIFF_BODY.format(warning=warning,
                                                       previous=snapshot / "previous.md")
                text += f"\n\n{m.EV_SNAPSHOT_DIFF_HEADING}\n{body}\n"
                print(m.EV_WARN_COUNTS_DECREASED_STDERR.format(warning=warning), file=sys.stderr)
        if structured is not None:
            structured_path = snapshot / "evidence.json"
            structured_path.write_text(structured, encoding="utf-8")
            text += "\n\n" + m.EV_STRUCTURED_FOOTER.format(path=structured_path) + "\n"
        text += "\n" + m.EV_SNAPSHOT_FOOTER.format(path=snapshot / "evidence.md") + "\n"
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
        raise EvidenceWriteError(m.EV_ERR_WRITE_FAILED.format(path=path, detail=e)) from e
    return WrittenEvidence(path.resolve(), snapshot / "evidence.md",
                           snapshot / "evidence.json" if structured is not None else None)
