"""Normalize session records from three agents into events.

A user request states intent, an assistant statement is self-reported, and a
tool result
only means something together with its call and exit status: the three are
stored apart and
none stands in for another. One extractor per format; a new agent is one table
entry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .diagnostics import SessionDiagnostics
from .models import Event, EventKind
from .text import blocks_text, is_injected, user_text
from .timewindow import epoch_ms, parse_ts


class _Collector:
    """Collect the events of one record, validating the tool name and call id
    shape.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.unparsed_message = False

    def add(self, kind: str, text, call_id=None) -> None:
        if kind == EventKind.TOOL_CALL and not isinstance(text.get("name"), str):
            raise ValueError("tool name must be a string")
        if call_id is not None and not isinstance(call_id, (str, int)):
            raise ValueError("tool call ID must be a string or integer")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        if text.strip() or kind == EventKind.TOOL_RESULT:
            self.events.append(Event(kind=kind, text=text, call_id=call_id))


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _claude_events(row: dict, out: _Collector) -> None:
    # api-request-blob stores copies of messages sent to the API, without event
    # times.
    # Only actual conversation records can supply text or tool evidence.
    if row.get("type") not in ("user", "assistant"):
        return
    content = _dict(row.get("message")).get("content")
    if row.get("type") == "user":
        txt = user_text(content)
        if not is_injected(txt):
            out.add(EventKind.USER_REQUEST, txt)
    elif row.get("type") == "assistant":
        out.add(EventKind.ASSISTANT_STATEMENT, blocks_text(content))
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            out.add(
                EventKind.TOOL_CALL,
                {"name": block.get("name"), "input": block.get("input")},
                block.get("id"),
            )
        elif block.get("type") == "tool_result":
            out.add(
                EventKind.TOOL_RESULT,
                {
                    "content": block.get("content"),
                    "is_error": block.get("is_error"),
                },
                block.get("tool_use_id"),
            )


def _codex_message(role: str, content, out: _Collector) -> None:
    """Normalize Codex's input/output text blocks without treating media as
    missing text.

    Unknown or malformed blocks mark a partial message, while readable siblings
    survive.
    System/developer messages are handled by the caller and never become user
    requests.
    """
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        out.unparsed_message = True
        return
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            out.unparsed_message = True
            continue
        kind = block.get("type")
        if kind in ("text", "input_text", "output_text"):
            if isinstance(block.get("text"), str):
                blocks.append({"type": "text", "text": block["text"]})
            else:
                out.unparsed_message = True
        elif kind not in (
            "image",
            "image_url",
            "input_image",
            "audio",
            "input_audio",
            "output_audio",
        ):
            out.unparsed_message = True
    if role == "user":
        text = user_text(blocks)
        if not is_injected(text):
            out.add(EventKind.USER_REQUEST, text)
    else:
        out.add(EventKind.ASSISTANT_STATEMENT, blocks_text(blocks))


def _codex_events(row: dict, out: _Collector) -> None:
    payload = _dict(row.get("payload"))
    kind = payload.get("type")
    if row.get("type") == "event_msg":
        # Recognize unsupported text records only to report the coverage gap.
        out.unparsed_message = kind in ("user_message", "agent_message")
    elif row.get("type") == "response_item":
        if kind == "message" and payload.get("role") in ("user", "assistant"):
            _codex_message(payload["role"], payload.get("content"), out)
        elif kind in ("function_call", "custom_tool_call"):
            out.add(
                EventKind.TOOL_CALL,
                {
                    "name": payload.get("name"),
                    "input": payload.get("arguments", payload.get("input")),
                },
                payload.get("call_id"),
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            out.add(
                EventKind.TOOL_RESULT,
                payload.get("output", ""),
                payload.get("call_id"),
            )


def _dsh_events(row: dict, out: _Collector) -> None:
    data = _dict(row.get("data"))
    kind = row.get("type")
    if kind == "user/message":
        txt = user_text(data.get("content"))
        if not is_injected(txt):
            out.add(EventKind.USER_REQUEST, txt)
    elif kind == "assistant/message":
        out.add(
            EventKind.ASSISTANT_STATEMENT,
            blocks_text(_dict(data.get("message")).get("content")),
        )
    elif kind == "tool/call":
        out.add(
            EventKind.TOOL_CALL,
            {"name": data.get("name"), "input": data.get("arguments")},
            data.get("call_id", data.get("id")),
        )
    elif kind == "tool/result":
        out.add(EventKind.TOOL_RESULT, data, data.get("call_id", data.get("id")))


EXTRACTORS = {
    "claude-code": _claude_events,
    "codex": _codex_events,
    "dsh": _dsh_events,
}


def record_events(row: dict, agent: str, diagnostics: SessionDiagnostics | None = None) -> list[Event]:
    """Keep claims and tool results separate; neither proves a report-level
    outcome.
    """
    out = _Collector()
    EXTRACTORS.get(agent, _dsh_events)(row, out)
    if diagnostics is not None:
        diagnostics.user_messages += sum(e.kind == EventKind.USER_REQUEST for e in out.events)
        diagnostics.assistant_messages += sum(e.kind == EventKind.ASSISTANT_STATEMENT for e in out.events)
        diagnostics.unparsed_messages += int(out.unparsed_message)
    return out.events


def record_timestamp(row: dict, agent: str, tz: timezone) -> datetime | None:
    if agent == "dsh":
        return epoch_ms(row.get("time"), tz) or parse_ts(row.get("createdAt"), tz)
    return parse_ts(row.get("timestamp"), tz)


def record_cwd(row: dict, agent: str) -> str | None:
    """Codex puts cwd in the session_meta payload; the other formats put it on
    the record.
    """
    if agent == "codex" and row.get("type") == "session_meta":
        return _dict(row.get("payload")).get("cwd")
    return row.get("cwd")
