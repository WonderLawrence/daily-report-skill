"""Read saved evidence by exact identity and field, with bounded, resumable output.

No collection, network calls or writes happen here. Receipts point to one immutable
snapshot; this reader never guesses the latest file or searches Markdown prose.
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from . import messages as m
from .models import (ArtifactKind, Commit, CommitRef, DirtyState, Event, FetchFailure,
                     FetchResult, PeriodCommit, PullRequest, PullRequestDetails, Session)
from .text import short_id

DEFAULT_PAGE_ITEMS = 10
DEFAULT_PAGE_CHARS = 4000
DEFAULT_MAX_CHARS = 8000
MIN_MAX_CHARS = 1000


def _object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(m.ER_ERR_READ_JSON.format(path=path, detail=exc)) from exc
    if not isinstance(value, dict):
        raise ValueError(m.ER_ERR_TOP_LEVEL.format(path=path))
    return value


def load_input(path: Path) -> tuple[Path, dict]:
    """Accept a snapshot or a receipt, following at most one explicit local path."""
    path = path.expanduser().resolve()
    value = _object(path)
    if value.get("kind") == "evidence_receipt":
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError(m.ER_ERR_RECEIPT_SCHEMA)
        target = value.get("evidence_path")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(m.ER_ERR_RECEIPT_PATH)
        target = Path(target).expanduser()
        path = (target if target.is_absolute() else path.parent / target).resolve()
        value = _object(path)
        if value.get("kind") == "evidence_receipt":
            raise ValueError(m.ER_ERR_RECEIPT_RECURSIVE)
    return path, value


def artifact_kind(value: dict) -> str:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError(m.ER_ERR_EVIDENCE_SCHEMA)
    if "sessions" in value and "selected_prs" not in value:
        return ArtifactKind.COLLECTION
    if "selected_prs" in value and "sessions" not in value:
        return ArtifactKind.PR_DETAILS
    raise ValueError(m.ER_ERR_KIND_UNKNOWN)


def _field(value, path: str):
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise ValueError(m.ER_ERR_FIELD_UNKNOWN.format(
                    part=part, available=", ".join(current)))
            current = current[part]
        elif isinstance(current, list):
            if not part.isdecimal() or int(part) >= len(current):
                raise ValueError(m.ER_ERR_INDEX_INVALID.format(part=part, length=len(current)))
            current = current[int(part)]
        else:
            raise ValueError(m.ER_ERR_SCALAR_LOOKUP.format(part=part))
    return current


def _select(document: dict, pr: str | None, session: str | None) -> dict:
    if pr is None and session is None:
        return document
    name, key = ("pull_requests", pr) if pr is not None else ("sessions", session)
    records = _field(document, name)
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError(m.ER_ERR_LIST_SHAPE.format(name=name))
    matches = [r for r in records if key == r.get("id") or (pr is not None and key == r.get("url"))]
    if not matches and session is not None:
        matches = [r for r in records if isinstance(r.get("id"), str) and short_id(r["id"]) == key]
    if len(matches) != 1:
        detail = m.ER_DETAIL_NOT_FOUND if not matches else m.ER_DETAIL_AMBIGUOUS
        raise ValueError(m.ER_ERR_KEY_NOT_FOUND.format(name=name, key=key, detail=detail))
    return matches[0]


def _type(value) -> str:
    return ("null" if value is None else "object" if isinstance(value, dict)
            else "array" if isinstance(value, list) else "string" if isinstance(value, str)
            else "boolean" if isinstance(value, bool) else "number")


def _describe(value: dict) -> dict:
    # Default inspection lists shapes, not all session events or all PR descriptions.
    metadata = {key: value[key] for key in
                ("schema_version", "generated_at", "start", "end_exclusive", "status", "summary",
                 "id", "agent", "project", "title", "retrieved_at") if key in value}
    shapes = {key: {"type": _type(item),
                    **({"length": len(item)} if isinstance(item, (dict, list, str)) else {})}
              for key, item in value.items()}
    return {"metadata": metadata, "fields": shapes}


def _projection(value: list, names: list[str], field: str, kind: str) -> list[dict]:
    """Validate even empty known lists, so a misspelled field never becomes []."""
    models = {"pull_requests": PullRequest if kind == ArtifactKind.COLLECTION else PullRequestDetails,
              "commits": Commit, "sessions": Session, "events": Event,
              "associated_commits": CommitRef, "period_commits": PeriodCommit,
              "git_fetch": FetchResult, "failures": FetchFailure, "dirty": DirtyState}
    model = models.get(field.split(".")[-1])
    available = {f.name for f in fields(model)} if model else {
        key for item in value if isinstance(item, dict) for key in item}
    unknown = set(names) - available
    if unknown:
        raise ValueError(m.ER_ERR_PROJECTION_UNKNOWN.format(
            unknown=", ".join(sorted(unknown)), available=", ".join(sorted(available))))
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(m.ER_ERR_PROJECTION_SHAPE)
        result.append({name: _field(item, name) for name in names})
    return result


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _page(value, base: dict, offset: int, limit: int | None, max_chars: int) -> dict:
    kind = _type(value)
    sequence = isinstance(value, (list, dict, str))
    total = len(value) if sequence else 1
    if offset > total or (not sequence and offset):
        raise ValueError(m.ER_ERR_PAGE_OFFSET.format(offset=offset, total=total))
    limit = limit if limit is not None else (
        DEFAULT_PAGE_CHARS if isinstance(value, str) else DEFAULT_PAGE_ITEMS)

    def packet(data, count):
        next_offset = offset + count if offset + count < total else None
        return {**base, "value_type": kind, "total": total, "offset": offset,
                "returned": count, "next_offset": next_offset, "data": data}

    if not sequence:
        result = packet(value, 1)
    elif isinstance(value, str):
        # Escapes may take more than one output character. Fit the encoded JSON, not
        # merely the input text length, and return a character offset for exact resumption.
        low, high = 0, min(limit, total - offset)
        while low < high:
            middle = (low + high + 1) // 2
            if len(encode(packet(value[offset:offset + middle], middle))) <= max_chars:
                low = middle
            else:
                high = middle - 1
        result = packet(value[offset:offset + low], low)
        if low == 0 and offset < total:
            raise ValueError(m.ER_ERR_PAGE_BUDGET)
    else:
        items = list(value.items()) if isinstance(value, dict) else value
        data = {} if isinstance(value, dict) else []
        result = packet(data, 0)
        for index in range(offset, min(offset + limit, total)):
            candidate = ({**data, items[index][0]: items[index][1]} if isinstance(value, dict)
                         else [*data, items[index]])
            trial = packet(candidate, index - offset + 1)
            if len(encode(trial)) > max_chars:
                if not data:
                    raise ValueError(m.ER_ERR_PAGE_ITEM_BUDGET.format(index=index))
                break
            data, result = candidate, trial
    if len(encode(result)) > max_chars:
        raise ValueError(m.ER_ERR_PAGE_OVERSIZE)
    return result


def read_page(path: Path, *, pr: str | None = None, session: str | None = None,
              field: str | None = None, projection: list[str] | None = None,
              offset: int = 0, limit: int | None = None,
              max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    if pr is not None and session is not None:
        raise ValueError(m.ER_ERR_PR_SESSION_CONFLICT)
    if offset < 0 or (limit is not None and limit < 1) or max_chars < MIN_MAX_CHARS:
        raise ValueError(m.ER_ERR_OFFSET_LIMIT.format(min_chars=MIN_MAX_CHARS))
    source, document = load_input(path)
    kind = artifact_kind(document)
    selected = _select(document, pr, session)
    base = {"evidence_path": str(source), "artifact_kind": kind,
            "pr": pr, "session": session, "field": field}
    if field is None:
        if projection or offset or limit is not None:
            raise ValueError(m.ER_ERR_FIELDS_NEEDS_FIELD)
        result = {**base, **_describe(selected)}
        if len(encode(result)) > max_chars:
            raise ValueError(m.ER_ERR_METADATA_BUDGET)
        return result
    value = _field(selected, field)
    if projection is not None:
        if not isinstance(value, list) or not projection or any(not n for n in projection):
            raise ValueError(m.ER_ERR_PROJECTION_NEEDS_FIELD)
        value = _projection(value, projection, field, kind)
        base["fields"] = projection
    return _page(value, base, offset, limit, max_chars)
