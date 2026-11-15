"""Session discovery and parsing for Claude Code, Codex and DSH."""
import json
import os
import subprocess
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from collections import Counter

from helpers import (sessions, source_sessions, text, write_jsonl, stderr_of,
                     SessionFixture, claude_rows, SessionDiagnostics, TZ, PERIOD,
                     NOON, IDE_BLOCK, END, run, render, make_report)


def codex_message(role, content, timestamp=NOON):
    return {"timestamp": timestamp.isoformat(), "type": "response_item",
            "payload": {"type": "message", "role": role, "content": content}}


def codex_tool_rows():
    return [
        {"timestamp": NOON.isoformat(), "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                     "arguments": '{"cmd":"fixture"}'}},
        {"timestamp": NOON.isoformat(), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "c1", "output": "fixture output"}},
    ]


class TestUserText(unittest.TestCase):
    def test_approval_transcript_blocks_are_not_reclassified_as_real_requests(self):
        blocks = [{"type": "input_text", "text": value} for value in (
            "检查解析器的过滤规则", "The following is the Codex agent history added since review.",
            ">>> TRANSCRIPT DELTA START", "[1] user: copied request", "[2] tool: copied result",
            ">>> TRANSCRIPT DELTA END", ">>> APPROVAL REQUEST START", '{"command": "fixture"}',
            ">>> APPROVAL REQUEST END", "保留这里的真实补充请求")]
        events = sessions.record_events(codex_message("user", blocks), "codex")
        self.assertEqual([e.text for e in events], ["检查解析器的过滤规则 保留这里的真实补充请求"])
        self.assertEqual(sessions.record_events(codex_message("user", blocks[1:-1]), "codex"), [])

    def test_runtime_wrappers_are_filtered_without_losing_adjacent_real_requests(self):
        wrappers = ["# AGENTS.md instructions for /demo\ncontext",
                    "The following is the Codex agent history\napproval input",
                    "Current runtime context.\ncontext", "Time sampled while preparing input: today"]
        request = "检查 AGENTS.md instructions 的解析和 Current runtime context. 过滤"
        for wrapper in wrappers:
            for agent in ("codex", "claude-code", "dsh"):
                with self.subTest(agent=agent, wrapper=wrapper):
                    blocks = [{"type": "input_text" if agent == "codex" else "text", "text": value}
                              for value in (wrapper, request)]
                    row = (codex_message("user", blocks) if agent == "codex" else
                           {"type": "user", "message": {"content": blocks}} if agent == "claude-code" else
                           {"type": "user/message", "data": {"content": blocks}})
                    self.assertEqual([e.text for e in sessions.record_events(row, agent)], [request])
                    self.assertTrue(text.is_injected(" \n" + wrapper))
                    self.assertFalse(text.is_injected(request))

    def test_visible_request_survives_injected_blocks(self):
        """IDE context and the real request share a message, so blocks are judged one by one."""
        cases = [
            ([IDE_BLOCK, {"type": "text", "text": "排查 mempool 死锁"}],
             "排查 mempool 死锁", "the request next to an IDE block is kept"),
            ([IDE_BLOCK, {"type": "tool_result", "content": "x"}],
             "", "nothing but injected blocks leaves nothing"),
            ("  hello ", "hello", "a plain string is just trimmed"),
        ]
        for content, expected, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(text.user_text(content), expected, reason)

    def test_injected_text_is_recognized_as_noise(self):
        for value in ("", "<system-reminder>x", "Base directory for this skill: /x"):
            with self.subTest(text=value):
                self.assertTrue(text.is_injected(value))


class TestClaudeSessions(SessionFixture):
    def test_api_request_blobs_do_not_duplicate_tools_or_report_bad_timestamps(self):
        rows = claude_rows()
        blob = {"type": "api-request-blob", "hash": "fixture", "message": rows[1]["message"]}
        self.claude_file([*rows, blob])
        diag = SessionDiagnostics()
        (session,) = source_sessions("claude", PERIOD, diag)
        self.assertEqual([e.kind for e in session.events],
                         ["user_request", "assistant_statement", "tool_call"])
        self.assertEqual((diag.invalid_records, diag.failed_files, diag.ok_files), (0, 0, 1))
        # A timestamp on a transport copy must not turn it into conversation evidence either.
        self.assertEqual(sessions.record_events(dict(blob, timestamp=NOON.isoformat()), "claude-code"), [])

    def test_request_next_to_ide_block_is_kept(self):
        self.claude_file(claude_rows())
        (s,) = source_sessions("claude", PERIOD)
        self.assertEqual(s.users, ["排查死锁"])
        self.assertEqual((s.project, s.steps), ("/demo", 1))
        self.assertEqual(s.files, {"/demo/a.rs"})

    def test_old_file_is_skipped(self):
        self.claude_file(claude_rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        diag = SessionDiagnostics()
        self.assertEqual(source_sessions("claude", PERIOD, diag), [])
        self.assertEqual(diag.files, 0)

class TestCodexSessions(SessionFixture):
    def codex_file(self, rows, name="r"):
        path = self.home / f".codex/sessions/2026/10/16/{name}.jsonl"
        write_jsonl(path, rows)
        return path

    def rows(self):
        return [
            {"timestamp": "2026-10-15T23:50:00+08:00", "type": "session_meta",
             "payload": {"cwd": "/demo"}},
            {"timestamp": "2026-10-16T00:20:00+08:00", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "继续排查 mempool"}]}},
            {"timestamp": "2026-10-16T00:30:00+08:00", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell"}},
        ]

    def test_session_started_previous_day_is_found_with_cwd(self):
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "10" / "15" / "r.jsonl", self.rows())
        (s,) = source_sessions("codex", PERIOD)
        self.assertEqual(s.users, ["继续排查 mempool"])
        self.assertEqual(s.project, "/demo", "session_meta precedes the window, cwd still read")

    def test_stale_file_is_skipped(self):
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "10" / "01" / "r.jsonl",
                    self.rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        self.assertEqual(source_sessions("codex", PERIOD), [])

    def test_response_messages_survive_full_cli_collection_and_archive(self):
        """Six tool-bearing sessions must not mask the loss of 9 user + 22 assistant messages."""
        for session in range(6):
            rows = [codex_message("user", [{"type": "input_text", "text": f"request {session}-{i}"}])
                    for i in range(9)]
            rows += codex_tool_rows()
            rows += [codex_message("assistant", [{"type": "output_text", "text": f"reply {session}-{i}"}])
                     for i in range(22)]
            self.codex_file(rows, f"session-{session}")
        cfg = self.home / "config.json"
        cfg.write_text(json.dumps({"reports_dir": str(self.home / "reports"),
                                  "sources": {"git": False, "claude": False,
                                              "codex": True, "dsh": False}}))
        result = run(["--date", "2026-10-16"],
                     env={"HOME": str(self.home), "DAILY_REPORT_CONFIG": str(cfg)})
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.home / "reports/.evidence"
        snapshot = json.loads(next(directory.glob(".history/*/*/evidence.json")).read_text())
        self.assertEqual(len(snapshot["sessions"]), 6)
        for session in snapshot["sessions"]:
            events = session["events"]
            kinds = Counter(event["kind"] for event in events)
            self.assertEqual(kinds, {"user_request": 9, "assistant_statement": 22,
                                     "tool_call": 1, "tool_result": 1})
            self.assertEqual(len(session["users"]), 9)
            self.assertEqual(events[0]["line"], 1)
            self.assertEqual(events[-1]["line"], 33)
            self.assertTrue(all(event["source"].endswith(session["id"] + ".jsonl") for event in events))
            self.assertEqual(events[9]["call_id"], events[10]["call_id"])
        diag = snapshot["diagnostics"]["codex"]
        self.assertEqual((diag["user_messages"], diag["assistant_messages"], diag["unparsed_messages"]),
                         (54, 132, 0))
        self.assertEqual((diag["ok_files"], diag["failed_files"]), (6, 0))
        markdown = (directory / "20261016.md").read_text()
        self.assertIn("request 0-0", markdown)
        self.assertIn("reply 0-21", markdown)

    def test_response_message_strings_and_text_blocks_respect_time_window(self):
        rows = [
            codex_message("user", [{"type": "input_text", "text": "new request"}]),
            codex_message("assistant", [{"type": "output_text", "text": "new reply"}]),
            codex_message("user", [{"type": "text", "text": "text block"}]),
            codex_message("user", "string request"),
            codex_message("assistant", "string reply"),
            codex_message("assistant", "next day", timestamp=END),
        ]
        self.codex_file(rows)
        diag = SessionDiagnostics()
        (session,) = source_sessions("codex", PERIOD, diag)
        self.assertEqual([event.text for event in session.events],
                         ["new request", "new reply", "text block", "string request", "string reply"])
        self.assertEqual((diag.user_messages, diag.assistant_messages, diag.unparsed_messages), (3, 2, 0))

    def test_event_msg_text_is_diagnosed_without_entering_evidence(self):
        unsupported = [
            {"timestamp": NOON.isoformat(), "type": "event_msg",
             "payload": {"type": "user_message", "message": "unsupported request"}},
            {"timestamp": NOON.isoformat(), "type": "event_msg",
             "payload": {"type": "agent_message", "message": "unsupported reply"}},
        ]
        cases = [
            ("unsupported only", [], []),
            ("tools survive", codex_tool_rows(), []),
            ("current text survives", [codex_message("user", "current request")], ["current request"]),
        ]
        for name, readable, expected_texts in cases:
            with self.subTest(case=name):
                self.codex_file(unsupported + readable)
                diagnostics = {}
                (found, _), warning = stderr_of(
                    sessions.collect_sessions, PERIOD, {"codex"}, [], 30, diagnostics)
                diag = diagnostics["codex"]
                self.assertEqual((diag.unparsed_messages, diag.ok_files, diag.failed_files), (2, 0, 1))
                self.assertEqual([e.text for session in found for e in session.events
                                  if e.kind in ("user_request", "assistant_statement")], expected_texts)
                self.assertIn("格式不支持", warning)
                self.assertIn("Codex 文字仅支持 response_item/message", warning)
                self.assertEqual(bool(found), bool(readable))

    def test_event_msg_metadata_does_not_trigger_text_format_warning(self):
        self.codex_file([
            {"timestamp": NOON.isoformat(), "type": "event_msg",
             "payload": {"type": "token_count", "info": {}}},
            codex_message("user", "current request"),
        ])
        diagnostics = {}
        (found, _), warning = stderr_of(sessions.collect_sessions, PERIOD, {"codex"}, [], 30, diagnostics)
        self.assertEqual(found[0].users, ["current request"])
        self.assertEqual(diagnostics["codex"].unparsed_messages, 0)
        self.assertEqual(warning, "")

    def test_injections_and_media_are_skipped_without_losing_adjacent_request(self):
        self.codex_file([
            codex_message("system", "system text"),
            codex_message("developer", "developer text"),
            codex_message("user", [{"type": "input_text", "text": IDE_BLOCK["text"]},
                                   {"type": "input_image", "image_url": "fixture://image"},
                                   {"type": "input_text", "text": "检查 RPC 空结果"}]),
            codex_message("user", [{"type": "input_text", "text": IDE_BLOCK["text"]}]),
            codex_message("user", [{"type": "input_image", "image_url": "fixture://image"}]),
            codex_message("assistant", []),
        ])
        diag = SessionDiagnostics()
        (session,) = source_sessions("codex", PERIOD, diag)
        self.assertEqual(session.users, ["检查 RPC 空结果"])
        self.assertEqual([event.text for event in session.events], ["检查 RPC 空结果"])
        self.assertEqual((diag.unparsed_messages, diag.failed_files), (0, 0))

    def test_unknown_message_content_warns_even_when_tools_parse_successfully(self):
        self.codex_file([
            codex_message("user", [{"type": "future_text", "body": "unreadable request"}]),
            *codex_tool_rows(),
        ])
        diagnostics = {}
        (found, _), warning = stderr_of(sessions.collect_sessions, PERIOD, {"codex"}, [], 30, diagnostics)
        self.assertEqual(len(found), 1)
        self.assertEqual([e.kind for e in found[0].events], ["tool_call", "tool_result"])
        diag = diagnostics["codex"]
        self.assertEqual((diag.unparsed_messages, diag.ok_files, diag.failed_files), (1, 0, 1))
        self.assertIn("1 条文字消息未能完整解析", warning)
        self.assertEqual(render.coverage_status(diag), "读取或解析失败")
        output = render.render(make_report(sessions=found, diagnostics=diagnostics))
        self.assertIn("文字消息解析不完整 1", output)

    def test_malformed_blocks_preserve_readable_siblings_and_other_messages(self):
        self.codex_file([
            codex_message("user", [{"type": "input_text", "text": "readable request"},
                                   {"type": "input_text", "text": {"bad": "shape"}}]),
            codex_message("assistant", {"changed": "container"}),
            codex_message("assistant", [{"type": "output_text", "text": "x" * 600}]),
            *codex_tool_rows(),
        ])
        diag = SessionDiagnostics()
        (session,) = source_sessions("codex", PERIOD, diag)
        self.assertEqual(session.users, ["readable request"])
        self.assertEqual(session.events[1].text, "x" * 600, "full evidence must not be truncated")
        self.assertEqual((diag.user_messages, diag.assistant_messages, diag.unparsed_messages), (1, 1, 2))
        self.assertEqual(diag.failed_files, 1)

    def test_unreadable_message_without_timestamp_remains_a_diagnostic_gap(self):
        row = codex_message("assistant", {"changed": "container"})
        row.pop("timestamp")
        self.codex_file([row, *codex_tool_rows()])
        diag = SessionDiagnostics()
        source_sessions("codex", PERIOD, diag)
        self.assertEqual((diag.unparsed_messages, diag.invalid_records, diag.failed_files), (1, 1, 1))

class TestDshSessions(SessionFixture):
    def test_parses_compressed_session(self):
        ms = int(NOON.timestamp() * 1000)
        rows = [
            {"type": "session", "cwd": "/demo"},
            {"type": "user/message", "time": ms,
             "data": {"content": [IDE_BLOCK, {"type": "text", "text": "查 DSH 会话"}]}},
            {"type": "tool/call", "time": ms + 1000,
             "data": {"name": "Read", "arguments": json.dumps({"file_path": "/demo/a.rs"})}},
        ]
        raw = "\n".join(json.dumps(r) for r in rows).encode()
        packed = subprocess.run(["zstd", "-q", "-c"], input=raw, capture_output=True, check=True).stdout
        f = self.home / ".dsh" / "sessions" / "-demo" / "session-abc" / "x.jsonl.zstd"
        f.parent.mkdir(parents=True)
        f.write_bytes(packed)
        os.utime(f, (NOON.timestamp(), NOON.timestamp()))
        (s,) = source_sessions("dsh", PERIOD)
        self.assertEqual(s.users, ["查 DSH 会话"])
        self.assertEqual((s.project, s.id, s.steps), ("/demo", "session-abc", 1))

class TestCompressedReader(SessionFixture):
    def test_a_noisy_decompressor_cannot_deadlock_the_reader(self):
        """zstd stderr is never read, so an undrained pipe would hang on a corrupt archive."""
        stub = self.home / "bin" / "zstd"
        stub.parent.mkdir(parents=True)
        stub.write_text("#!/usr/bin/env python3\n"
                        "import sys\n"
                        "sys.stderr.write('warning: corrupt frame\\n' * 12000)\n"
                        "print('{\"type\":\"user/message\",\"time\":1,"
                        "\"data\":{\"content\":\"hi\"}}')\n")
        stub.chmod(0o755)
        archive = self.home / "x.jsonl.zstd"
        archive.write_bytes(b"not really zstd")

        diag = SessionDiagnostics()
        with patch.dict(os.environ, {"PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}"}):
            rows = _read_with_timeout(archive, diag)
        self.assertEqual(len(rows), 1)


def _read_with_timeout(path, diag, seconds=20):
    """Read in a worker thread: a deadlock shows up as a timeout instead of hanging the suite."""
    result = []
    worker = threading.Thread(target=lambda: result.extend(sessions.session_records(path, diag)),
                              daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AssertionError(f"session_records did not finish within {seconds}s (deadlock)")
    return result


class TestFormatDrift(SessionFixture):
    def drifted_rows(self):
        # Simulate a renamed field (timestamp -> ts) so no event is recognized
        return [dict(r, ts=r.pop("timestamp")) for r in claude_rows()]

    def test_touched_file_without_events_warns(self):
        self.claude_file(self.drifted_rows())
        (found, truncated), err = stderr_of(
            sessions.collect_sessions, PERIOD, {"claude"}, [], 30)
        self.assertEqual(found, [])
        self.assertIn("[warn] claude", err)
        self.assertIn("格式可能变了", err)

    def test_events_without_content_warns(self):
        _, err = stderr_of(sessions.warn_format_drift, "codex", "~/.codex/sessions",
                           SessionDiagnostics(files=1, events=5), 0)
        self.assertIn("[warn] codex", err)

    def test_healthy_parse_is_silent(self):
        self.claude_file(claude_rows())
        (found, _), err = stderr_of(
            sessions.collect_sessions, PERIOD, {"claude"}, [], 30)
        self.assertEqual(len(found), 1)
        self.assertEqual(err, "")

    def test_no_modified_files_is_silent(self):
        self.claude_file(self.drifted_rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        _, err = stderr_of(sessions.collect_sessions, PERIOD, {"claude"}, [], 30)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
