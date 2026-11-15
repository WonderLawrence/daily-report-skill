"""Tool evidence must retain its association, or report an explicit gap."""
import json
import os
import shutil
import subprocess
import unittest

from helpers import (END, NOON, PERIOD, SessionDiagnostics, SessionFixture,
                     evidence, make_report, make_session, render, sessions,
                     source_sessions, write_jsonl)
from daily_report.models import Event
from daily_report.session_events import record_events


def dsh_call(call_id="call-a"):
    return {"type": "tool/call", "time": int(NOON.timestamp() * 1000),
            "data": {"turn": 1, "step": 1, "callId": call_id, "name": "shell",
                     "arguments": {"cmd": f"test-{call_id}"}}}


def dsh_result(call_id="call-a"):
    return {"type": "tool/result", "time": int(NOON.timestamp() * 1000) + 1000,
            "data": {"turn": 1, "step": 1, "message": {
                "role": "tool", "id": "result-message-id", "toolCallId": call_id,
                "source": {"callId": call_id}, "content": f"result-{call_id}",
                "isError": False}, "meta": {}}}


class TestToolIdentity(SessionFixture):
    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd CLI")
    def test_dsh_v4_pairs_survive_compression_parsing_and_json(self):
        rows = [dsh_call("a"), dsh_call("b"), dsh_result("b"), dsh_result("a")]
        # Message IDs and legacy-looking fields must never override call IDs.
        for row in rows:
            row["data"].update(id="unrelated-id", call_id="unrelated-call-id")
        packed = subprocess.run(["zstd", "-q", "-c"],
                                input="\n".join(json.dumps(r) for r in rows).encode(),
                                capture_output=True, check=True).stdout
        path = self.home / ".dsh/sessions/-demo/session-v4/session.v4.jsonl.zstd"
        path.parent.mkdir(parents=True)
        path.write_bytes(packed)
        os.utime(path, (NOON.timestamp(), NOON.timestamp()))
        diag = SessionDiagnostics()
        (session,) = source_sessions("dsh", PERIOD, diag)
        saved = evidence.json_evidence(session, False)["events"]
        self.assertEqual([e["call_id"] for e in saved], ["a", "b", "b", "a"])
        self.assertEqual([e["line"] for e in saved], [1, 2, 3, 4])
        self.assertTrue(all(e["source"] == str(path) for e in saved))
        self.assertEqual((diag.ok_files, diag.failed_files, diag.missing_call_ids), (1, 0, 0))
        self.assertEqual(diag.issues, [])
        output = render.render(make_report(sessions=[session]))
        self.assertLess(output.index("test-b"), output.index("result-b"))
        self.assertLess(output.index("result-b"), output.index("test-a"))
        self.assertLess(output.index("test-a"), output.index("result-a"))

    def test_result_prefers_tool_call_id_then_source_call_id(self):
        for primary, expected in [("primary", "primary"), (0, 0), (None, "fallback"),
                                  ("", "fallback"), ("  ", "fallback"), ({}, "fallback")]:
            with self.subTest(primary=primary):
                row = dsh_result()
                row["data"]["message"].update(toolCallId=primary, source={"callId": "fallback"})
                diag = SessionDiagnostics()
                (event,) = record_events(row, "dsh", diag)
                self.assertEqual(event.call_id, expected)
                self.assertEqual(diag.missing_call_ids, 0)

    def test_message_and_legacy_ids_do_not_invent_associations(self):
        call, result = dsh_call(), dsh_result()
        del call["data"]["callId"]
        del result["data"]["message"]["toolCallId"]
        result["data"]["message"]["source"] = {"id": "source-message-id"}
        for row in (call, result):
            row["data"].update(id="unrelated-id", call_id="unrelated-call-id")
            diag = SessionDiagnostics()
            (event,) = record_events(row, "dsh", diag)
            self.assertIsNone(event.call_id)
            self.assertEqual(diag.missing_call_ids, 1)

    def test_unusable_ids_keep_tool_content_and_report_the_gap(self):
        for value in (None, "", "  ", False, True, [], {}, 1.5):
            with self.subTest(value=value):
                diag = SessionDiagnostics()
                events = [event for row in (dsh_call(value), dsh_result(value))
                          for event in record_events(row, "dsh", diag)]
                self.assertEqual([e.kind for e in events], ["tool_call", "tool_result"])
                self.assertTrue(all(e.call_id is None for e in events))
                self.assertIn("shell", events[0].text)
                self.assertIn("isError", events[1].text)
                self.assertEqual(diag.missing_call_ids, 2)

    def test_missing_ids_surface_in_json_markdown_and_file_coverage(self):
        path = write_jsonl(self.home / "missing.jsonl", [dsh_call(None), dsh_result(None)])
        diag = SessionDiagnostics()
        session = sessions.parse_session_file(path, "dsh", PERIOD, diag, "/demo")
        self.assertEqual(len(session.events), 2)
        self.assertEqual((diag.ok_files, diag.failed_files, diag.invalid_records, diag.missing_call_ids),
                         (0, 1, 0, 2))
        self.assertEqual([(i.line, i.kind, i.record_type) for i in diag.issues],
                         [(1, "missing_call_id", "tool/call"), (2, "missing_call_id", "tool/result")])
        self.assertTrue(all(i.source == str(path) for i in diag.issues))
        saved = evidence.json_evidence({"sessions": [session], "diagnostics": {"dsh": diag}}, False)
        self.assertEqual(saved["diagnostics"]["dsh"]["missing_call_ids"], 2)
        output = render.render(make_report(sessions=[session], diagnostics={"dsh": diag}))
        self.assertIn("工具关联 ID 缺失或不可用 2", output)
        self.assertIn("missing.jsonl:2 · missing_call_id", output)

    def test_issue_limit_does_not_limit_missing_event_count(self):
        diag = SessionDiagnostics()
        for number in range(2):
            path = write_jsonl(self.home / f"missing-{number}.jsonl", [dsh_call(None)] * 22)
            session = sessions.parse_session_file(path, "dsh", PERIOD, diag, "/demo")
            self.assertEqual(len(session.events), 22)
        self.assertEqual((diag.missing_call_ids, diag.failed_files, len(diag.issues)), (44, 2, 20))

    def test_out_of_period_missing_ids_are_not_counted(self):
        outside = dsh_call(None)
        outside["time"] = int(END.timestamp() * 1000)
        path = write_jsonl(self.home / "scope.jsonl", [dsh_call(), outside])
        diag = SessionDiagnostics()
        session = sessions.parse_session_file(path, "dsh", PERIOD, diag, "/demo")
        self.assertEqual(len(session.events), 1)
        self.assertEqual((diag.missing_call_ids, diag.ok_files), (0, 1))

    def test_missing_timestamp_still_reports_missing_identity(self):
        row = dsh_call(None)
        del row["time"]
        path = write_jsonl(self.home / "no-time.jsonl", [dsh_call(), row])
        diag = SessionDiagnostics()
        session = sessions.parse_session_file(path, "dsh", PERIOD, diag, "/demo")
        self.assertEqual(len(session.events), 1)
        self.assertEqual((diag.missing_call_ids, diag.invalid_records, diag.failed_files), (1, 1, 1))
        self.assertEqual([i.kind for i in diag.issues], ["missing_timestamp", "missing_call_id"])

    def test_missing_ids_are_diagnosed_for_codex_and_claude_too(self):
        cases = {
            "codex": [{"type": "response_item", "payload": payload} for payload in (
                {"type": "function_call", "name": "shell", "arguments": "test"},
                {"type": "function_call_output", "output": "result"})],
            "claude-code": [{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "shell", "input": {}},
                {"type": "tool_result", "content": "result"}]}}],
        }
        for agent, rows in cases.items():
            with self.subTest(agent=agent):
                diag = SessionDiagnostics()
                events = [e for row in rows for e in record_events(row, agent, diag)]
                self.assertEqual([e.kind for e in events], ["tool_call", "tool_result"])
                self.assertEqual(diag.missing_call_ids, 2)


class TestRenderedToolPairing(unittest.TestCase):
    def test_empty_ids_never_pair_but_integer_zero_does(self):
        for call_id in (None, "", "  ", False, [], {}):
            with self.subTest(call_id=call_id):
                session = make_session(events=[Event("tool_call", "unrelated-command", call_id, at=NOON),
                                               Event("tool_result", "retained-result", call_id, at=NOON)])
                output = render.render(make_report(sessions=[session]))
                self.assertNotIn("unrelated-command", output)
                self.assertIn("retained-result", output)
        session = make_session(events=[Event("tool_call", "zero-command", 0, at=NOON),
                                       Event("tool_result", "zero-result", 0, at=NOON)])
        output = render.render(make_report(sessions=[session]))
        self.assertIn("zero-command", output)
        self.assertIn("zero-result", output)

    def test_same_id_in_other_session_is_not_a_match(self):
        first = make_session(id="one", events=[Event("tool_call", "other-session-command", "same", at=NOON)])
        second = make_session(id="two", events=[Event("tool_result", "retained-result", "same", at=NOON)])
        output = render.render(make_report(sessions=[first, second]))
        self.assertNotIn("other-session-command", output)
        self.assertIn("retained-result", output)
