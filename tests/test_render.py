"""Rendering the evidence pack, and the record shapes it reads."""
import dataclasses
import unittest
from datetime import datetime, timedelta

from helpers import (evidence, render, make_commit, make_report, make_session, text,
                     TZ, START, NOON)
from daily_report.models import CommitRef, MessageStatus, PeriodCommit, Scope


class TestRender(unittest.TestCase):
    def session(self, users):
        return make_session(users=users, end=NOON + timedelta(minutes=5))

    def render(self, sessions, **kwargs):
        return render.render(make_report(sessions=sessions, **kwargs))

    def test_long_session_keeps_head_and_tail_of_user_messages(self):
        out = self.render([self.session([f"msg{i}" for i in range(6)])])
        for kept in ("msg0", "msg1", "msg4", "msg5"):
            self.assertIn(kept, out)
        for dropped in ("msg2", "msg3"):
            self.assertNotIn(dropped, out)
        self.assertIn("省略 2 条", out)

    def test_short_session_lists_everything(self):
        out = self.render([self.session(["a", "b", "c", "d"])])
        self.assertIn("a / b / c / d", out)
        self.assertNotIn("省略", out)

    def test_session_header_shows_short_id(self):
        sess = self.session(["hi"])
        sess.id = "1b6c7937-f8cf-4e4a-ac46-dd5a5c20381d"
        self.assertIn("· id 5c20381d", self.render([sess]))

    def test_single_day_session_header_keeps_time_only(self):
        out = self.render([self.session(["hi"])])
        self.assertIn("· 12:00–12:05 ·", out)

    def test_multiday_report_dates_same_day_sessions(self):
        for days in (2, 7):
            with self.subTest(days=days):
                out = render.render(make_report(sessions=[self.session(["hi"])],
                                                end=START + timedelta(days=days), days=days))
                self.assertIn("· 10-16 12:00–10-16 12:05 ·", out)

    def test_multiday_report_dates_both_ends_of_overnight_session(self):
        sess = self.session(["hi"])
        sess.end += timedelta(days=1)
        out = render.render(make_report(sessions=[sess], end=START + timedelta(days=7), days=7))
        self.assertIn("· 10-16 12:00–10-17 12:05 ·", out)

    def test_header_carries_generation_time(self):
        out = self.render([], generated=datetime(2026, 10, 18, 19, 5, tzinfo=TZ))
        self.assertIn("生成于 2026-10-18 19:05", out)

    def test_commit_without_local_copy_does_not_look_empty(self):
        """A commit made elsewhere has platform metadata only; never render it as +0/-0."""
        remote = make_commit("fix: 在别处提交", repo="not-cloned", repo_path="",
                             full_sha="a" * 40, sha="a" * 7, files=None, added=None,
                             deleted=None)
        out = render.render(make_report(commits=[remote]))
        self.assertIn("改动规模未知：本地没有该提交", out)
        self.assertIn("本地未找到该提交，仅平台元数据", out)
        self.assertNotIn("+0/-0", out)

    def test_writing_hint_points_to_single_source(self):
        out = self.render([], period_word="周报")
        self.assertIn("写周报前按 references/writing.md", out)
        self.assertIn("SKILL.md", out)

class TestRecordShapes(unittest.TestCase):
    def test_scope_renders_from_fields_and_keeps_its_on_disk_keys(self):
        """Scope replaced a raw dict: the section reads fields, the JSON keys stay the same."""
        scope = Scope(git=True, repos=["node"], sessions=["claude"], dirty=True,
                      only_repos=["/p/node"])
        output = render.render(make_report(scope=scope))
        self.assertIn("- Git：node", output)
        self.assertIn("- 会话：claude", output)
        self.assertIn("已扫描（运行时快照）", output)
        self.assertIn("- 仅关注项目：/p/node", output)
        self.assertEqual(list(evidence.json_evidence(scope, False)),
                         ["git", "repos", "sessions", "dirty", "only_repos"])

    def test_report_without_scope_renders_no_scope_section(self):
        self.assertNotIn("## 采集范围", render.render(make_report()))


    def test_period_commit_extends_a_commit_reference(self):
        """PeriodCommit inherits CommitRef, so the reference fields exist in one place only."""
        ref = CommitRef(full_sha="a" * 40, repo_path="/p", at="t", authored="t",
                        author="me", subject="s")
        commit = PeriodCommit(**(vars(ref) | {"message": "body"}), message_status=MessageStatus.OK)
        self.assertIsInstance(commit, CommitRef)
        self.assertEqual([f.name for f in dataclasses.fields(commit)][:len(dataclasses.fields(ref))],
                         [f.name for f in dataclasses.fields(ref)])

class TestShortId(unittest.TestCase):
    UUID = "0199aaaa-b74b-7172-9bf3-05172815cd55"

    def test_the_tail_of_the_embedded_uuid_identifies_a_session(self):
        """Every agent wraps the UUID differently, and UUIDv7 prefixes repeat, so the
        last 8 chars of the embedded UUID are what locates a session file."""
        cases = [
            (self.UUID, "2815cd55", "Claude Code uses a bare UUID"),
            (f"rollout-2026-03-20T10-54-47-{self.UUID}", "2815cd55", "Codex prefixes a date"),
            (f"session-{self.UUID}", "2815cd55", "DSH prefixes a word"),
            ("abcdef0123456789", "23456789", "a non UUID id falls back to its tail"),
            ("i", "i", "a short id is returned as is"),
        ]
        for identity, expected, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(text.short_id(identity), expected, reason)

    def test_uuid_v7_sessions_started_close_together_do_not_collide(self):
        a = "019d092a-b74b-7172-9bf3-05172815cd55"
        b = "019d092a-c001-7000-8000-0000deadbeef"  # same timestamp prefix
        self.assertNotEqual(text.short_id(a), text.short_id(b))


if __name__ == "__main__":
    unittest.main()
