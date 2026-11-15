"""Text handling for session bodies and identifiers; no data source or configuration here."""
from __future__ import annotations

import re

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Skill expansions, subagent replies and system injections all masquerade as "user
# messages"; for a work report they are noise.
NOISE_PREFIXES = (
    "Base directory for this skill",
    "Caveat:",
    "Another Claude session sent a message",
    "This session is being continued",
    "<",
)


def one_line(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def blocks_text(content) -> str:
    """Keep only the visible body of `content` (str or block list); drop thinking/tool_use."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def user_text(content) -> str:
    """Visible body of a user message, dropping injected blocks one by one.

    IDE context (<ide_opened_file> and friends) often shares a message with the real
    request, so judging the whole message would swallow the request along with it.
    """
    if isinstance(content, list):
        parts = [b.get("text", "").strip() for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if not is_injected(p))
    return blocks_text(content).strip()


def is_injected(txt: str) -> bool:
    if not txt:
        return True
    if txt.startswith(NOISE_PREFIXES):
        return True
    return "<agent-message" in txt or "This session is being continued" in txt[:200]


def short_id(session_id: str) -> str:
    """Session file names carry a UUID; the last 8 chars are enough to `find` the file.

    Not the first 8: Codex uses UUIDv7, whose prefix is a timestamp, so sessions started
    around the same moment would collide.
    """
    match = UUID_RE.search(session_id)
    return (match.group(0) if match else session_id)[-8:]
