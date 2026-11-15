"""Evidence coverage, provenance and snapshot regressions."""
import json
import os
import shutil
import subprocess
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import (evidence, gitlog, render, sessions, run, source_sessions, SessionFixture,
                     START, END, PERIOD, NOON, claude_rows, write_jsonl, stderr_of, make_session,
                     make_report, make_commit, period_of, DirtyState, GitDiagnostics,
                     SessionDiagnostics)


def session(identity, project, day=0, steps=0):
    moment = NOON + timedelta(days=day)
    return make_session(identity, project, start=moment, end=moment, users=[identity],
                        tools=Counter(Read=steps), steps=steps)


def fixture_source(records):
    """A fake session source: skip file scanning and hand back parsed sessions."""
    return SimpleNamespace(name="claude", agent="claude-code", location="fixture",
                           collect=lambda period, diag=None: records)


class TestSessionCoverage(SessionFixture):
    def test_budget_retains_index_and_shares_dates_and_projects(self):
        records = [session("coding", "/a", steps=100), session("more-coding", "/a", steps=90),
                   session("decision", "/b"), session("next-day", "/c", day=1)]
        with patch.object(sessions, "SESSION_SOURCES", (fixture_source(records),)):
            diagnostics = {}
            result, truncated = sessions.collect_sessions(
                period_of(START, END + timedelta(days=1)), {"claude"}, [], 3, diagnostics)
        self.assertTrue(truncated)
        self.assertEqual(len(result), 4)
        self.assertEqual({s.id for s in result if s.detailed},
                         {"coding", "decision", "next-day"})
        output = render.render(make_report(sessions=result, truncated=truncated,
                                           end=END + timedelta(days=1),
                                           diagnostics=diagnostics))
        self.assertIn("id `more-coding`", output)
        self.assertIn("摘要省略，可回查", output)
        self.assertEqual(diagnostics["claude"].omitted, 1)

    def test_bad_file_does_not_hide_healthy_source(self):
        self.claude_file(claude_rows())
        broken = self.home / ".claude/projects/-demo/broken.jsonl"
        broken.write_text('{"timestamp":')
        diagnostics = {}
        found, _ = sessions.collect_sessions(PERIOD, {"claude"}, [], 30, diagnostics)
        self.assertEqual(len(found), 1)
        diag = diagnostics["claude"]
        self.assertEqual((diag.scanned, diag.ok_files, diag.failed_files), (2, 1, 1))
        self.assertEqual(diag.invalid_records, 1)
        self.assertEqual(render.coverage_status(diag), "部分失败")

    def test_drifted_file_detected_alongside_healthy_file(self):
        self.claude_file(claude_rows())
        write_jsonl(self.home / ".claude/projects/-demo/new-format.jsonl",
                    [{"ts": NOON.isoformat(), "new_message": "not understood"}])
        diag = SessionDiagnostics()
        self.assertEqual(len(source_sessions("claude", PERIOD, diag)), 1)
        self.assertEqual(diag.failed_files, 1)
        self.assertEqual(diag.unrecognized_files, 1)

    def test_malformed_tool_record_keeps_other_events_and_marks_partial(self):
        rows = claude_rows()
        rows.append({"type": "assistant", "timestamp": NOON.isoformat(),
                     "message": {"content": [{"type": "tool_use", "name": {"new": "shape"}}]}})
        self.claude_file(rows)
        diag = SessionDiagnostics()
        (s,) = source_sessions("claude", PERIOD, diag)
        self.assertEqual(s.users, ["排查死锁"])
        self.assertEqual(diag.invalid_records, 1)
        self.assertEqual(diag.failed_files, 1)

    def test_missing_timestamp_in_partial_file_is_reported(self):
        rows = claude_rows()
        rows[1]["ts"] = rows[1].pop("timestamp")
        self.claude_file(rows)
        diag = SessionDiagnostics()
        (s,) = source_sessions("claude", PERIOD, diag)
        self.assertEqual(s.users, ["排查死锁"])
        self.assertEqual(diag.failed_files, 1)

    def test_git_read_failure_is_not_reported_as_no_work(self):
        diag = GitDiagnostics(scanned=1)
        result, _ = stderr_of(gitlog.git_commits, self.home, PERIOD, diag)
        self.assertEqual(result, [])
        self.assertEqual(diag.failed_files, 1)
        self.assertEqual(render.coverage_status(diag), "读取或解析失败")

    def test_unreadable_file_is_counted(self):
        path = self.claude_file(claude_rows())
        diag = SessionDiagnostics()
        with patch.object(Path, "open", side_effect=PermissionError("fixture")):
            result = sessions.parse_session_file(path, "claude-code", PERIOD, diag, "demo")
        self.assertIsNone(result)
        self.assertEqual(diag.read_errors, 1)
        self.assertEqual(diag.failed_files, 1)

    def test_unavailable_empty_and_disabled_are_distinct(self):
        diag = SessionDiagnostics()
        source_sessions("codex", PERIOD, diag)
        unavailable = render.coverage_status(diag)
        (self.home / ".codex/sessions").mkdir(parents=True)
        diag = SessionDiagnostics()
        source_sessions("codex", PERIOD, diag)
        self.assertNotEqual(unavailable, render.coverage_status(diag))
        self.assertEqual(render.coverage_status(diag), "所选范围无记录")
        self.assertEqual(render.coverage_status(SessionDiagnostics(disabled=True)), "未启用")


class TestDiagnosticsRendering(unittest.TestCase):
    def test_each_source_renders_only_its_own_counters(self):
        """A session row carries no commit counter and a git row no session counters."""
        output = render.render(make_report(diagnostics={
            "git": GitDiagnostics(scanned=2, ok_files=2, commits=3),
            "claude": SessionDiagnostics(scanned=5, ok_files=5, sessions=4, omitted=1)}))
        git_line = next(l for l in output.splitlines() if l.startswith("- git："))
        claude_line = next(l for l in output.splitlines() if l.startswith("- claude："))
        self.assertIn("提交 3", git_line)
        self.assertNotIn("会话", git_line)
        self.assertIn("会话 4", claude_line)
        self.assertNotIn("提交", claude_line)
        self.assertIn("摘要省略 1", claude_line)

    def test_only_the_git_side_reports_a_fetch_gap(self):
        self.assertEqual(render.coverage_status(GitDiagnostics(ok_files=1, fetch_failed=1)),
                         "远程同步不完整，使用可读的本地记录")
        self.assertEqual(render.coverage_status(SessionDiagnostics(ok_files=1, sessions=2)),
                         "正常")


class TestSessionIndexHints(SessionFixture):
    def parse_texts(self, requests=(), statements=()):
        rows = [{"type": "user", "timestamp": NOON.isoformat(), "cwd": "/demo",
                 "message": {"content": text}} for text in requests]
        rows += [{"type": "assistant", "timestamp": NOON.isoformat(), "cwd": "/demo",
                  "message": {"content": [{"type": "text", "text": text}]}}
                 for text in statements]
        self.claude_file(rows)
        (record,) = source_sessions("claude", PERIOD)
        return record

    def test_continuation_uses_analysis_instead_of_generic_request(self):
        record = self.parse_texts(["继续。"], ["比较全局锁与分片锁，尚未实施。"])
        hint, basis = render.session_index_hint(record)
        self.assertEqual(basis, "assistant_statement")
        self.assertIn("比较全局锁", hint)
        self.assertEqual(record.users, ["继续。"])
        self.assertEqual(len(record.events), 2)

    def test_late_acknowledgments_do_not_hide_earlier_analysis(self):
        record = self.parse_texts(statements=["调查发现 RPC 空结果编码不一致。",
                                             "好的", "收到", "完成了"])
        self.assertNotIn("调查", " ".join(record.concl))
        hint, basis = render.session_index_hint(record)
        self.assertEqual(basis, "assistant_statement")
        self.assertIn("RPC 空结果编码", hint)
        self.assertEqual(len(record.events), 4)

    def test_requests_keep_the_task_instead_of_acknowledgments(self):
        record = self.parse_texts(["好的", "排查 RPC 空结果编码", "继续", "谢谢！"],
                                  ["已查看实现。"])
        self.assertEqual(render.session_index_hint(record),
                         ("排查 RPC 空结果编码", "user_request"))
        self.assertEqual(len(record.users), 4)

    def test_generic_or_injected_text_requires_screening(self):
        record = self.parse_texts(["继续"], ["Base directory for this skill: /fixture", "OK!"])
        self.assertEqual(render.session_index_hint(record), ("", None))
        output = render.render(make_report(sessions=[record]))
        index = output.split("### 完整会话索引", 1)[1].split("### [claude-code]", 1)[0]
        self.assertIn("需初筛，暂不列候选", index)
        self.assertNotIn("Base directory", index)
        self.assertEqual(len(record.events), 3)


class TestEventProvenance(SessionFixture):
    def test_codex_preserves_requests_claims_and_failed_tool_output(self):
        path = self.home / ".codex/sessions/2026/10/15/test.jsonl"
        rows = [
            {"timestamp": "2026-10-15T23:50:00+08:00", "type": "session_meta",
             "payload": {"cwd": "/demo"}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "检查测试"}]}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1",
                         "arguments": '{"cmd":"python3 -m unittest"}'}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1",
                         "output": "FAILED (failures=1), exit_code=1"}},
            {"timestamp": NOON.isoformat(), "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "完成了"}]}},
            {"timestamp": END.isoformat(), "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "下一天的结论"}]}},
        ]
        write_jsonl(path, rows)
        (s,) = source_sessions("codex", PERIOD)
        self.assertEqual([e.kind for e in s.events],
                         ["user_request", "tool_call", "tool_result", "assistant_statement"])
        result = s.events[2]
        self.assertEqual((result.source, result.line, result.call_id), (str(path), 4, "c1"))
        output = render.render(make_report(sessions=[s]))
        self.assertIn("助手陈述（未独立核验）", output)
        self.assertIn("FAILED (failures=1)", output)
        self.assertIn("python3 -m unittest", output)
        self.assertNotIn("下一天的结论", output)

    def test_assistant_only_continuation_is_collected(self):
        rows = claude_rows()
        rows[1]["message"]["content"] = [{"type": "text", "text": "方案评估：暂缓实施"}]
        self.claude_file(rows[1:])
        (s,) = source_sessions("claude", PERIOD)
        self.assertEqual(s.steps, 0)
        self.assertEqual(s.events[0].kind, "assistant_statement")

    def test_claude_tool_results_keep_call_identity(self):
        rows = claude_rows()
        rows[1]["message"]["content"][1]["id"] = "tool-1"
        rows.append({"timestamp": NOON.isoformat(), "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-1", "is_error": True,
             "content": "test failed"}]}})
        self.claude_file(rows)
        (s,) = source_sessions("claude", PERIOD)
        result = s.events[-1]
        self.assertEqual((result.kind, result.call_id, result.line),
                         ("tool_result", "tool-1", 3))
        self.assertTrue(json.loads(result.text)["is_error"])

    def test_structured_redaction_preserves_valid_json_and_middle_events(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        rows = claude_rows()
        rows[1]["message"]["content"] = [{"type": "text", "text": f"中途放弃方案；{secret}"}]
        self.claude_file(rows)
        (s,) = source_sessions("claude", PERIOD)
        encoded = json.dumps(evidence.json_evidence(s, True), ensure_ascii=False)
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
        result, _ = sessions.collect_sessions(PERIOD, {"dsh"}, [], 30, diagnostics)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].events), 3)
        self.assertEqual(len({e.source for e in result[0].events}), 2)
        self.assertEqual(diagnostics["dsh"].sessions, 1)
        self.assertEqual(diagnostics["dsh"].ok_files, 2)
        output = render.render(make_report(sessions=result))
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

    def test_recollection_losing_commits_is_reported(self):
        """The guard must watch commits too: a reworded section title once silenced it."""
        first = render.render(make_report(commits=[make_commit("feat: a", full_sha="a" * 40),
                                                   make_commit("feat: b", full_sha="b" * 40)]))
        second = render.render(make_report(commits=[make_commit("feat: a", full_sha="a" * 40)]))
        path = self.home / "latest.md"
        self.assertIsNone(evidence.write_evidence(path, first, False))
        self.assertIsNone(evidence.write_evidence(path, second, True))
        self.assertIn("commits 2 → 1", path.read_text())

    def test_unserializable_evidence_fails_before_touching_the_filesystem(self):
        """A payload json cannot encode must not produce a traceback or a stray snapshot."""
        path = self.home / "latest.md"
        error = evidence.write_evidence(path, "text", False, {"bad": object()})
        self.assertIn("无法序列化结构化证据", error)
        self.assertFalse(path.exists())
        self.assertFalse((self.home / ".history").exists())

    def test_refusing_overwrite_has_no_side_effects(self):
        path = self.home / "old.md"
        path.write_text("old")
        self.assertIsNotNone(evidence.write_evidence(path, "new", False))
        self.assertEqual(path.read_text(), "old")
        self.assertFalse((self.home / ".history").exists())

    def test_failed_publish_preserves_existing_evidence(self):
        path = self.home / "old.md"
        path.write_text("old evidence")
        with patch.object(evidence.os, "replace", side_effect=OSError("fixture failure")):
            error = evidence.write_evidence(path, "new evidence", True, {"sessions": []})
        self.assertIsNotNone(error)
        self.assertEqual(path.read_text(), "old evidence")
        self.assertEqual(next(self.home.glob(".history/*/*/previous.md")).read_text(), "old evidence")

    def test_retention_of_latest_views_does_not_delete_archives(self):
        for name in ("a.md", "b.md"):
            self.assertIsNone(evidence.write_evidence(self.home / name, name, False))
        evidence.prune_evidence(self.home, 1)
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

    def test_tool_only_session_keeps_evidence_but_requires_screening(self):
        rows = claude_rows()[1:]
        rows[0]["message"]["content"] = [{"type": "tool_use", "id": "t1",
            "name": "Bash", "input": {"command": "python3 -m unittest test_rpc"}}]
        rows.append({"type": "user", "timestamp": NOON.isoformat(), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "FAILED: test_empty_rpc_result"}]}})
        self.claude_file(rows)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = next((self.home / "reports/.evidence").glob(".history/*/*/evidence.json"))
        data = json.loads(snapshot.read_text())
        record = data["sessions"][0]
        self.assertEqual([event["kind"] for event in record["events"]], ["tool_call", "tool_result"])
        self.assertIn("test_empty_rpc_result", record["events"][1]["text"])
        markdown = (snapshot.parent / "evidence.md").read_text()
        index = markdown.split("### 完整会话索引", 1)[1].split("### [claude-code]", 1)[0]
        self.assertIn("需初筛，暂不列候选", index)
        self.assertIn("id `s1`", index)


class TestDirtyMeaning(unittest.TestCase):
    def test_old_commit_does_not_claim_old_uncommitted_changes(self):
        output = render.render(make_report(
            dirty=[DirtyState(repo="demo", files=1, age_days=180)]))
        self.assertIn("改动存在时长未知", output)
        self.assertNotIn("⚠️遗留", output)
        self.assertIn("最近一次提交 180 天前", output)


if __name__ == "__main__":
    unittest.main()
