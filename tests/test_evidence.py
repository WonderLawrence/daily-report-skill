"""Evidence coverage, provenance and snapshot regressions."""
import json
import os
import shutil
import subprocess
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_collect import (collect, run, SessionFixture, START, END, NOON, TZ,
                          claude_rows, write_jsonl, stderr_of)


def session(identity, project, day=0, steps=0):
    return {"agent": "claude-code", "project": project, "id": identity,
            "start": NOON + timedelta(days=day), "end": NOON + timedelta(days=day),
            "users": [identity], "concl": [], "tools": Counter(Read=steps),
            "files": set(), "steps": steps}


class TestSessionCoverage(SessionFixture):
    def test_budget_retains_index_and_shares_dates_and_projects(self):
        records = [session("coding", "/a", steps=100), session("more-coding", "/a", steps=90),
                   session("decision", "/b"), session("next-day", "/c", day=1)]
        parsers = (("claude", lambda *args: records, "fixture"),
                   ("codex", collect.codex_sessions, "fixture"),
                   ("dsh", collect.dsh_sessions, "fixture"))
        with patch.object(collect, "SESSION_SOURCES", parsers):
            diagnostics = {}
            result, truncated = collect.collect_sessions(
                START, END + timedelta(days=1), TZ, {"codex": False, "dsh": False},
                False, [], 3, diagnostics)
        self.assertTrue(truncated)
        self.assertEqual(len(result), 4)
        self.assertEqual({s["id"] for s in result if s["detailed"]},
                         {"coding", "decision", "next-day"})
        output = collect.render(START, END + timedelta(days=1), {}, [], result, truncated,
                                diagnostics=diagnostics)
        self.assertIn("id `more-coding`", output)
        self.assertIn("摘要省略，可回查", output)
        self.assertEqual(diagnostics["claude"]["omitted"], 1)

    def test_bad_file_does_not_hide_healthy_source(self):
        self.claude_file(claude_rows())
        broken = self.home / ".claude/projects/-demo/broken.jsonl"
        broken.write_text('{"timestamp":')
        diagnostics = {}
        sessions, _ = collect.collect_sessions(START, END, TZ,
            {"codex": False, "dsh": False}, False, [], 30, diagnostics)
        self.assertEqual(len(sessions), 1)
        diag = diagnostics["claude"]
        self.assertEqual((diag["scanned"], diag["ok_files"], diag["failed_files"]), (2, 1, 1))
        self.assertEqual(diag["invalid_records"], 1)
        self.assertEqual(collect.coverage_status(diag), "部分失败")

    def test_drifted_file_detected_alongside_healthy_file(self):
        self.claude_file(claude_rows())
        write_jsonl(self.home / ".claude/projects/-demo/new-format.jsonl",
                    [{"ts": NOON.isoformat(), "new_message": "not understood"}])
        diag = Counter()
        self.assertEqual(len(collect.claude_sessions(START, END, TZ, diag)), 1)
        self.assertEqual(diag["failed_files"], 1)
        self.assertEqual(diag["unrecognized_files"], 1)

    def test_malformed_tool_record_keeps_other_events_and_marks_partial(self):
        rows = claude_rows()
        rows.append({"type": "assistant", "timestamp": NOON.isoformat(),
                     "message": {"content": [{"type": "tool_use", "name": {"new": "shape"}}]}})
        self.claude_file(rows)
        diag = Counter()
        (s,) = collect.claude_sessions(START, END, TZ, diag)
        self.assertEqual(s["users"], ["排查死锁"])
        self.assertEqual(diag["invalid_records"], 1)
        self.assertEqual(diag["failed_files"], 1)

    def test_missing_timestamp_in_partial_file_is_reported(self):
        rows = claude_rows()
        rows[1]["ts"] = rows[1].pop("timestamp")
        self.claude_file(rows)
        diag = Counter()
        (s,) = collect.claude_sessions(START, END, TZ, diag)
        self.assertEqual(s["users"], ["排查死锁"])
        self.assertEqual(diag["failed_files"], 1)

    def test_git_read_failure_is_not_reported_as_no_work(self):
        diag = Counter(scanned=1)
        result, _ = stderr_of(collect.git_commits, self.home, START, END, diag)
        self.assertEqual(result, [])
        self.assertEqual(diag["failed_files"], 1)
        self.assertEqual(collect.coverage_status(diag), "读取或解析失败")

    def test_unreadable_file_is_counted(self):
        path = self.claude_file(claude_rows())
        diag = Counter()
        with patch.object(Path, "open", side_effect=PermissionError("fixture")):
            result = collect.parse_session_file(path, "claude-code", START, END, TZ, diag, "demo")
        self.assertIsNone(result)
        self.assertEqual(diag["read_errors"], 1)
        self.assertEqual(diag["failed_files"], 1)

    def test_unavailable_empty_and_disabled_are_distinct(self):
        diag = Counter()
        collect.codex_sessions(START, END, TZ, diag)
        unavailable = collect.coverage_status(diag)
        (self.home / ".codex/sessions").mkdir(parents=True)
        diag = Counter()
        collect.codex_sessions(START, END, TZ, diag)
        self.assertNotEqual(unavailable, collect.coverage_status(diag))
        self.assertEqual(collect.coverage_status(diag), "所选范围无记录")
        self.assertEqual(collect.coverage_status({"disabled": 1}), "未启用")


class TestEventProvenance(SessionFixture):
    def test_codex_preserves_requests_claims_and_failed_tool_output(self):
        path = self.home / ".codex/sessions/2026/10/15/test.jsonl"
        rows = [
            {"timestamp": "2026-10-15T23:50:00+08:00", "type": "session_meta",
             "payload": {"cwd": "/demo"}},
            {"timestamp": NOON.isoformat(), "type": "event_msg",
             "payload": {"type": "user_message", "message": "检查测试"}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                         "arguments": '{"cmd":"python3 -m unittest"}'}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": "FAILED (failures=1), exit_code=1"}},
            {"timestamp": NOON.isoformat(), "type": "event_msg",
             "payload": {"type": "agent_message", "message": "完成了"}},
            {"timestamp": END.isoformat(), "type": "event_msg",
             "payload": {"type": "agent_message", "message": "下一天的结论"}},
        ]
        write_jsonl(path, rows)
        (s,) = collect.codex_sessions(START, END, TZ)
        self.assertEqual([e["kind"] for e in s["events"]],
                         ["user_request", "tool_call", "tool_result", "assistant_statement"])
        result = s["events"][2]
        self.assertEqual((result["source"], result["line"], result["call_id"]), (str(path), 4, "c1"))
        output = collect.render(START, END, {}, [], [s], False)
        self.assertIn("助手陈述（未独立核验）", output)
        self.assertIn("FAILED (failures=1)", output)
        self.assertIn("python3 -m unittest", output)
        self.assertNotIn("下一天的结论", output)

    def test_assistant_only_continuation_is_collected(self):
        rows = claude_rows()
        rows[1]["message"]["content"] = [{"type": "text", "text": "方案评估：暂缓实施"}]
        self.claude_file(rows[1:])
        (s,) = collect.claude_sessions(START, END, TZ)
        self.assertEqual(s["steps"], 0)
        self.assertEqual(s["events"][0]["kind"], "assistant_statement")

    def test_claude_tool_results_keep_call_identity(self):
        rows = claude_rows()
        rows[1]["message"]["content"][1]["id"] = "tool-1"
        rows.append({"timestamp": NOON.isoformat(), "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True,
             "content": "test failed"}]}})
        self.claude_file(rows)
        (s,) = collect.claude_sessions(START, END, TZ)
        result = s["events"][-1]
        self.assertEqual((result["kind"], result["call_id"], result["line"]),
                         ("tool_result", "tool-1", 3))
        self.assertTrue(json.loads(result["text"])["is_error"])

    def test_structured_redaction_preserves_valid_json_and_middle_events(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        rows = claude_rows()
        rows[1]["message"]["content"] = [{"type": "text", "text": f"中途放弃方案；{secret}"}]
        self.claude_file(rows)
        (s,) = collect.claude_sessions(START, END, TZ)
        encoded = json.dumps(collect.json_evidence(s, True), ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertNotIn(secret, encoded)
        self.assertIn("中途放弃方案", decoded["events"][1]["text"])


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd CLI")
class TestDshFragments(SessionFixture):
    def test_fragments_keep_all_events_and_result_provenance(self):
        directory = self.home / ".dsh/sessions/-demo/session-shared"
        directory.mkdir(parents=True)
        ms = int(NOON.timestamp() * 1000)
        fragments = [
            [{"type": "session", "cwd": "/demo"},
             {"type": "user/message", "time": ms, "data": {"content": "评估方案"}},
             {"type": "tool/call", "time": ms + 1000,
              "data": {"id": "a", "name": "shell", "arguments": '{"cmd":"test"}'}}],
            [{"type": "session", "cwd": "/demo"},
             {"type": "tool/result", "time": ms + 2000,
              "data": {"id": "a", "output": "FAIL", "exit_code": 1}}],
        ]
        for i, rows in enumerate(fragments):
            raw = "\n".join(json.dumps(r) for r in rows).encode()
            data = subprocess.run(["zstd", "-q", "-c"], input=raw,
                                  capture_output=True, check=True).stdout
            path = directory / f"{i}.jsonl.zstd"
            path.write_bytes(data)
            os.utime(path, (NOON.timestamp(), NOON.timestamp()))
        diagnostics = {}
        result, _ = collect.collect_sessions(START, END, TZ,
            {"claude": False, "codex": False}, False, [], 30, diagnostics)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["events"]), 3)
        self.assertEqual(len({e["source"] for e in result[0]["events"]}), 2)
        self.assertEqual(diagnostics["dsh"]["sessions"], 1)
        self.assertEqual(diagnostics["dsh"]["ok_files"], 2)
        output = collect.render(START, END, {}, [], result, False)
        self.assertIn("FAIL", output)
        self.assertIn("对应工具调用", output)


class TestEvidenceSnapshots(SessionFixture):
    def cli(self, *args):
        cfg = self.home / "config.json"
        cfg.write_text(json.dumps({"reports_dir": str(self.home / "reports"),
                                  "sources": {"git": False, "claude": True,
                                              "codex": False, "dsh": False}}))
        return run(["--date", "2026-10-16", *args],
                   env={"HOME": str(self.home), "DAILY_REPORT_CONFIG": str(cfg)})

    def test_recollect_after_source_loss_preserves_snapshot_and_warns(self):
        source = self.claude_file(claude_rows())
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        directory = self.home / "reports/.evidence"
        saved = next(directory.glob(".history/*/*/evidence.json"))
        original = saved.read_text()
        self.assertEqual(len(json.loads(original)["sessions"]), 1)
        source.unlink()
        r = self.cli("--force", "--keep", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sessions 1 → 0", r.stderr)
        self.assertEqual(saved.read_text(), original)
        latest = (directory / "20261016.md").read_text()
        self.assertIn("重采证据数量减少", latest)
        self.assertEqual(len(list(directory.glob(".history/*/*/evidence.json"))), 2)
        self.assertTrue(any("排查死锁" in p.read_text()
                            for p in directory.glob(".history/*/*/previous.md")))

    def test_refusing_overwrite_has_no_side_effects(self):
        path = self.home / "old.md"
        path.write_text("old")
        self.assertIsNotNone(collect.write_evidence(path, "new", False))
        self.assertEqual(path.read_text(), "old")
        self.assertFalse((self.home / ".history").exists())

    def test_failed_publish_preserves_existing_evidence(self):
        path = self.home / "old.md"
        path.write_text("old evidence")
        with patch.object(collect.os, "replace", side_effect=OSError("fixture failure")):
            error = collect.write_evidence(path, "new evidence", True, {"sessions": []})
        self.assertIsNotNone(error)
        self.assertEqual(path.read_text(), "old evidence")
        self.assertEqual(next(self.home.glob(".history/*/*/previous.md")).read_text(), "old evidence")

    def test_retention_of_latest_views_does_not_delete_archives(self):
        for name in ("a.md", "b.md"):
            self.assertIsNone(collect.write_evidence(self.home / name, name, False))
        collect.prune_evidence(self.home, 1)
        self.assertEqual(len(list(self.home.glob("*.md"))), 1)
        self.assertEqual(len(list(self.home.glob(".history/*/*/evidence.md"))), 2)

    def test_cli_archive_redacts_all_events_and_reports_partial_failure(self):
        rows = claude_rows()
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        rows[0]["message"]["content"] = "中途问题 " + secret
        self.claude_file(rows)
        (self.home / ".claude/projects/-demo/broken.jsonl").write_text("{invalid")
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        snapshot = next((self.home / "reports/.evidence").glob(".history/*/*/evidence.json"))
        payload = json.loads(snapshot.read_text())
        self.assertEqual(payload["diagnostics"]["claude"]["failed_files"], 1)
        for path in snapshot.parent.iterdir():
            self.assertNotIn(secret, path.read_text())
        self.assertEqual(len(payload["sessions"][0]["events"]), 3)


class TestDirtyMeaning(unittest.TestCase):
    def test_old_commit_does_not_claim_old_uncommitted_changes(self):
        output = collect.render(START, END, {}, [], [], False,
                                dirty=[{"repo": "demo", "files": 1, "age_days": 180}])
        self.assertIn("改动存在时长未知", output)
        self.assertNotIn("⚠️遗留", output)
        self.assertIn("最近一次提交 180 天前", output)


if __name__ == "__main__":
    unittest.main()
