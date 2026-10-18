#!/usr/bin/env python3
"""collect.py 的回归测试（只用标准库 unittest）。

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect.py"

spec = importlib.util.spec_from_file_location("collect_mod", SCRIPT)
collect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collect)

TZ = timezone(timedelta(hours=8))


def run(args, env=None, cwd=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([sys.executable, str(SCRIPT)] + args,
                          capture_output=True, text=True, env=full_env, cwd=cwd)


def git(repo: Path, *args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, env=e, check=True)


def make_repo(base: Path, name="repo", user="Test User", email="dev@example.com") -> Path:
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", ".")
    git(repo, "config", "user.name", user)
    git(repo, "config", "user.email", email)
    return repo


def commit(repo: Path, msg: str, authored: str | None = None, committed: str | None = None,
           filename="f.txt"):
    (repo / filename).write_text(msg)
    git(repo, "add", ".")
    env = {}
    if authored:
        env["GIT_AUTHOR_DATE"] = authored
    if committed:
        env["GIT_COMMITTER_DATE"] = committed
    git(repo, "commit", "-q", "-m", msg, env=env)


class TestDedupeAndRevert(unittest.TestCase):
    def test_collapses_rebase_duplicates(self):
        base = {"repo": "r", "subject": "feat(x): a", "files": ["a.rs"],
                "added": 10, "deleted": 1, "at": "2026-10-13T10:00:00+08:00",
                "full_sha": "1", "sha": "1", "revert": False, "author": "x"}
        dup = dict(base, full_sha="2", sha="2")
        shown, extras = collect.collapse_duplicates([base, dup])
        self.assertEqual(len(shown), 1)
        self.assertEqual(extras, 1)
        self.assertEqual(shown[0]["copies"], 1)

    def test_keeps_distinct_commits(self):
        a = {"repo": "r", "subject": "feat(x): a", "files": ["a.rs"], "added": 1,
             "deleted": 0, "at": "2026-10-13T10:00:00+08:00", "full_sha": "1",
             "sha": "1", "revert": False, "author": "x"}
        b = dict(a, subject="feat(x): b", full_sha="2", sha="2")
        shown, extras = collect.collapse_duplicates([a, b])
        self.assertEqual((len(shown), extras), (2, 0))

    def test_revert_pairing_is_bidirectional(self):
        feat = {"repo": "r", "subject": 'feat: x', "files": ["a"], "added": 5,
                "deleted": 1, "at": "2026-10-16T10:00:00+08:00", "full_sha": "aa",
                "sha": "aa", "revert": False, "author": "x"}
        rev = dict(feat, subject='Revert "feat: x"', full_sha="bb", sha="bb", revert=True)
        collect.pair_reverts([feat, rev])
        self.assertEqual(feat["reverted_by"], "bb")
        self.assertEqual(rev["reverts_sha"], "aa")


class TestAnalysis(unittest.TestCase):
    def test_scope_summary(self):
        cs = [{"subject": "refactor(mempool): a"}, {"subject": "fix(mempool): b"},
              {"subject": "feat(cli): c"}, {"subject": "chore: d"}]
        rows = collect.scope_summary(cs)
        top = rows[0]
        self.assertEqual(top[0], "mempool")
        self.assertEqual(top[1], 2)

    def test_redact_masks_secrets(self):
        raw = ("ghp_abcdefghijklmnopqrstuvwxyz0123456789 sk-ABCDEFGHIJKLMNOPQRST "
               "10.1.2.3 password=hunter2secret AKIAABCDEFGHIJKLMNOP")
        out = collect.redact_text(raw)
        for leak in ("ghp_", "sk-ABC", "10.1.2.3", "hunter2secret", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(leak, out)

    def test_link_sessions_by_relative_path(self):
        cm = {"repo": "demo-node", "subject": "refactor(mempool): x",
              "files": ["crates/mempool/src/pool.rs"], "full_sha": "s1", "sha": "s1"}
        sess = {"project": "/Users/u/Project/demo-node", "files":
                {"/Users/u/Project/demo-node/crates/mempool/src/pool.rs"}}
        collect.link_sessions([sess], [cm])
        self.assertEqual(len(sess["related"]), 1)
        self.assertEqual(sess["related"][0]["sha"], "s1")

    def test_link_sessions_ignores_other_repo(self):
        cm = {"repo": "demo-node", "subject": "x", "files": ["a.rs"],
              "full_sha": "s1", "sha": "s1"}
        sess = {"project": "/Users/u/Project/other", "files": {"/Users/u/Project/other/a.rs"}}
        collect.link_sessions([sess], [cm])
        self.assertEqual(sess["related"], [])

    def test_mark_near_duplicates(self):
        s1 = {"agent": "claude-code", "project": "/p", "users": ["same prompt"],
              "start": datetime(2026, 10, 12, tzinfo=TZ)}
        s2 = dict(s1, start=datetime(2026, 10, 13, tzinfo=TZ))
        collect.mark_near_duplicates([s1, s2])
        self.assertEqual(s1["dup_n"], 2)
        self.assertEqual(s2["dup_n"], 2)

    def test_prune_evidence_keeps_newest(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for i in range(5):
                p = d / f"{i}.md"
                p.write_text("x")
                os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
            collect.prune_evidence(d, 2)
            self.assertEqual(sorted(p.name for p in d.glob("*.md")), ["3.md", "4.md"])


class TestGitCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_committer_date_not_author_date(self):
        repo = make_repo(self.base)
        # 作者时间在两周前，落地时间是今天
        commit(repo, "refactor(x): landed today", authored="2026-09-28T10:00:00+08:00",
               committed="2026-10-16T10:00:00+08:00")
        start = datetime(2026, 10, 16, tzinfo=TZ)
        got = collect.git_commits(repo, start, start + timedelta(days=1))
        self.assertEqual(len(got), 1, "按落地时间应被收录")
        self.assertTrue(got[0]["authored"].startswith("2026-09-28"))
        self.assertTrue(got[0]["at"].startswith("2026-10-16"))

    def test_excludes_stash(self):
        repo = make_repo(self.base)
        commit(repo, "feat: base", committed="2026-10-16T10:00:00+08:00")
        (repo / "f.txt").write_text("dirty")
        git(repo, "stash", "push", "-q", "-m", "wip")
        start = datetime(2026, 10, 16, tzinfo=TZ)
        got = collect.git_commits(repo, start, start + timedelta(days=2))
        self.assertFalse([c for c in got if c["subject"].startswith(("index on", "untracked files"))])

    def test_collapses_real_cherry_pick_copy(self):
        """生产路径（带 repo_path 的真实提交）会调用 git patch-id；曾因 bytes/text 混用而崩溃。"""
        repo = make_repo(self.base)
        commit(repo, "init", committed="2026-10-01T10:00:00+08:00", filename="base.txt")
        git(repo, "checkout", "-q", "-b", "topic")
        commit(repo, "fix(pool): demo", committed="2026-10-16T10:00:00+08:00")
        git(repo, "checkout", "-q", "-b", "copy", "HEAD~1")
        git(repo, "cherry-pick", "topic", env={"GIT_COMMITTER_DATE": "2026-10-16T11:00:00+08:00"})
        start = datetime(2026, 10, 16, tzinfo=TZ)
        commits = collect.git_commits(repo, start, start + timedelta(days=1))
        self.assertEqual(len(commits), 2)
        self.assertIsNotNone(collect.git_patch_id(repo, commits[0]["full_sha"]))
        shown, extras = collect.collapse_duplicates(commits)
        self.assertEqual((len(shown), extras), (1, 1))

    def test_week_window_includes_only_committer_range(self):
        repo = make_repo(self.base)
        commit(repo, "feat: in", committed="2026-10-15T10:00:00+08:00")
        commit(repo, "feat: out", committed="2026-10-01T10:00:00+08:00")
        start = datetime(2026, 10, 12, tzinfo=TZ)
        got = collect.git_commits(repo, start, start + timedelta(days=7))
        self.assertEqual([c["subject"] for c in got], ["feat: in"])


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / ".gitconfig").write_text(
            "[user]\n\tname = Test User\n\temail = dev@example.com\n")
        self.cfg = self.base / "cfg.json"
        self.cfg.write_text(json.dumps({
            "author_emails": ["dev@example.com"],
            "repos_root": str(self.base),
            "reports_dir": str(self.base / "reports"),
            "max_sessions": 5,
        }))
        self.env = {"HOME": str(self.base), "DAILY_REPORT_CONFIG": str(self.cfg)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_bad_date_is_friendly(self):
        r = run(["--date", "2026-13-01"], env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("YYYY-MM-DD", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_alias_identity_warns(self):
        repo = make_repo(self.base, "aliasrepo", user="Test User", email="alt@example.com")
        commit(repo, "feat: by alt email", committed="2026-10-16T10:00:00+08:00")
        r = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions", "--print"],
                env=self.env)
        self.assertIn("同名", r.stderr)
        self.assertIn("alt@example.com", r.stderr)

    def test_author_filter_drops_other_authors(self):
        repo = make_repo(self.base, "teamrepo")
        commit(repo, "feat: mine", committed="2026-10-16T10:00:00+08:00")
        git(repo, "config", "user.name", "Colleague")
        git(repo, "config", "user.email", "colleague@example.com")
        commit(repo, "feat: theirs", committed="2026-10-16T11:00:00+08:00")
        r = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions", "--print"],
                env=self.env)
        self.assertIn("feat: mine", r.stdout)
        self.assertNotIn("feat: theirs", r.stdout)

    def test_redaction_on_by_default(self):
        repo = make_repo(self.base, "tokrepo")
        commit(repo, "fix: rotate ghp_abcdefghijklmnopqrstuvwxyz0123456789",
               committed="2026-10-16T10:00:00+08:00")
        r = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions", "--print"],
                env=self.env)
        self.assertIn("[REDACTED:github-token]", r.stdout)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz", r.stdout)

    def test_evidence_written_and_keep_pruned(self):
        repo = make_repo(self.base, "keeprepo")
        for i, day in enumerate(("14", "15", "16")):
            commit(repo, f"feat: d{i}", committed=f"2026-10-{day}T10:00:00+08:00")
        for day in ("14", "15", "16"):
            r = run(["--date", f"2026-10-{day}", "--repo", str(repo), "--no-sessions",
                     "--keep", "1"], env=self.env)
            self.assertEqual(r.returncode, 0, r.stderr)
        left = sorted(p.name for p in (self.base / "reports" / ".evidence").glob("*.md"))
        self.assertEqual(left, ["20261016.md"])

    def test_force_required_to_overwrite(self):
        repo = make_repo(self.base, "forceRepo")
        commit(repo, "feat: x", committed="2026-10-16T10:00:00+08:00")
        run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions"], env=self.env)
        r = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions"], env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--force", r.stderr)
        r2 = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions", "--force"],
                 env=self.env)
        self.assertEqual(r2.returncode, 0)

    def test_week_flag_conflicts_with_date(self):
        r = run(["--week", "--date", "2026-10-16"], env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("不能", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_week_and_yesterday_run_end_to_end(self):
        r = run(["--week", "--no-git", "--no-sessions", "--print"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("写周报前", r.stdout)
        r = run(["--date", "yesterday", "--no-git", "--no-sessions", "--print"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cherry_pick_copies_do_not_crash_cli(self):
        repo = make_repo(self.base, "dups")
        commit(repo, "init", committed="2026-10-01T10:00:00+08:00", filename="base.txt")
        git(repo, "checkout", "-q", "-b", "topic")
        commit(repo, "fix(pool): demo", committed="2026-10-16T10:00:00+08:00")
        git(repo, "checkout", "-q", "-b", "copy", "HEAD~1")
        git(repo, "cherry-pick", "topic", env={"GIT_COMMITTER_DATE": "2026-10-16T11:00:00+08:00"})
        r = run(["--date", "2026-10-16", "--repo", str(repo), "--no-sessions", "--print"],
                env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("已折叠 1 个 rebase 副本", r.stdout)


START = datetime(2026, 10, 16, tzinfo=TZ)
END = START + timedelta(days=1)
NOON = datetime(2026, 10, 16, 12, 0, tzinfo=TZ)
IDE_BLOCK = {"type": "text", "text": "<ide_opened_file>The user opened /demo/a.rs</ide_opened_file>"}


def write_jsonl(path: Path, rows, mtime: datetime = NOON) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def stderr_of(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


class TestUserText(unittest.TestCase):
    def test_ide_block_does_not_swallow_real_request(self):
        content = [IDE_BLOCK, {"type": "text", "text": "排查 mempool 死锁"}]
        self.assertEqual(collect.user_text(content), "排查 mempool 死锁")

    def test_only_injected_blocks_yield_nothing(self):
        content = [IDE_BLOCK, {"type": "tool_result", "content": "x"}]
        self.assertTrue(collect.is_injected(collect.user_text(content)))

    def test_plain_string_unchanged(self):
        self.assertEqual(collect.user_text("  hello "), "hello")
        self.assertTrue(collect.is_injected(collect.user_text("<system-reminder>x")))


class SessionFixture(unittest.TestCase):
    """把 collect.HOME 指到临时目录，用合成会话文件测解析器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._home = collect.HOME
        collect.HOME = self.home
        collect._PROJECT_MAP = None

    def tearDown(self):
        collect.HOME = self._home
        collect._PROJECT_MAP = None
        self.tmp.cleanup()

    def claude_file(self, rows, mtime=NOON):
        return write_jsonl(self.home / ".claude" / "projects" / "-demo" / "s1.jsonl", rows, mtime)


def claude_rows():
    return [
        {"type": "user", "timestamp": "2026-10-16T10:00:00+08:00", "cwd": "/demo",
         "message": {"role": "user", "content": [IDE_BLOCK, {"type": "text", "text": "排查死锁"}]}},
        {"type": "assistant", "timestamp": "2026-10-16T10:01:00+08:00", "cwd": "/demo",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "根因是接收端锁被持有"},
             {"type": "tool_use", "name": "Read", "input": {"file_path": "/demo/a.rs"}}]}},
    ]


class TestClaudeSessions(SessionFixture):
    def test_request_next_to_ide_block_is_kept(self):
        self.claude_file(claude_rows())
        (s,) = collect.claude_sessions(START, END, TZ)
        self.assertEqual(s["users"], ["排查死锁"])
        self.assertEqual((s["project"], s["steps"]), ("/demo", 1))
        self.assertEqual(s["files"], {"/demo/a.rs"})

    def test_old_file_is_skipped(self):
        self.claude_file(claude_rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        diag = Counter()
        self.assertEqual(collect.claude_sessions(START, END, TZ, diag), [])
        self.assertEqual(diag["files"], 0)


class TestCodexSessions(SessionFixture):
    def rows(self):
        return [
            {"timestamp": "2026-10-15T23:50:00+08:00", "type": "session_meta",
             "payload": {"cwd": "/demo"}},
            {"timestamp": "2026-10-16T00:20:00+08:00", "type": "event_msg",
             "payload": {"type": "user_message", "message": "继续排查 mempool"}},
            {"timestamp": "2026-10-16T00:30:00+08:00", "type": "response_item",
             "payload": {"type": "function_call", "name": "shell"}},
        ]

    def test_session_started_previous_day_is_found_with_cwd(self):
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "10" / "15" / "r.jsonl", self.rows())
        (s,) = collect.codex_sessions(START, END, TZ)
        self.assertEqual(s["users"], ["继续排查 mempool"])
        self.assertEqual(s["project"], "/demo", "session_meta 在窗口前，cwd 仍要取到")

    def test_stale_file_is_skipped(self):
        write_jsonl(self.home / ".codex" / "sessions" / "2026" / "10" / "01" / "r.jsonl",
                    self.rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        self.assertEqual(collect.codex_sessions(START, END, TZ), [])


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd CLI")
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
        (s,) = collect.dsh_sessions(START, END, TZ)
        self.assertEqual(s["users"], ["查 DSH 会话"])
        self.assertEqual((s["project"], s["id"], s["steps"]), ("/demo", "session-abc", 1))


class TestFormatDrift(SessionFixture):
    def drifted_rows(self):
        # 模拟字段改名：timestamp -> ts，解析器认不出任何事件
        return [dict(r, ts=r.pop("timestamp")) for r in claude_rows()]

    def test_touched_file_without_events_warns(self):
        self.claude_file(self.drifted_rows())
        sources = {"claude": True, "codex": False, "dsh": False}
        (sessions, truncated), err = stderr_of(
            collect.collect_sessions, START, END, TZ, sources, False, [], 30)
        self.assertEqual(sessions, [])
        self.assertIn("[warn] claude", err)
        self.assertIn("格式可能变了", err)

    def test_events_without_content_warns(self):
        _, err = stderr_of(collect.warn_format_drift, "codex", "~/.codex/sessions",
                           Counter(files=1, events=5), 0)
        self.assertIn("[warn] codex", err)

    def test_healthy_parse_is_silent(self):
        self.claude_file(claude_rows())
        sources = {"claude": True, "codex": False, "dsh": False}
        (sessions, _), err = stderr_of(
            collect.collect_sessions, START, END, TZ, sources, False, [], 30)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(err, "")

    def test_no_modified_files_is_silent(self):
        self.claude_file(self.drifted_rows(), mtime=datetime(2026, 10, 1, tzinfo=TZ))
        sources = {"claude": True, "codex": False, "dsh": False}
        _, err = stderr_of(collect.collect_sessions, START, END, TZ, sources, False, [], 30)
        self.assertEqual(err, "")


class TestResolvePeriod(unittest.TestCase):
    THU = date(2026, 10, 15)

    def bounds(self, *args, **kwargs):
        start, end = collect.resolve_period(*args, **kwargs)
        return start.date(), (end - timedelta(seconds=1)).date()

    def test_this_week_runs_monday_to_today(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="this", today=self.THU),
                         (date(2026, 10, 12), date(2026, 10, 15)))

    def test_this_week_on_sunday_is_full_week(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="this", today=date(2026, 10, 18)),
                         (date(2026, 10, 12), date(2026, 10, 18)))

    def test_last_week_is_previous_monday_to_sunday(self):
        self.assertEqual(self.bounds(None, 1, TZ, week="last", today=self.THU),
                         (date(2026, 10, 5), date(2026, 10, 11)))

    def test_yesterday_and_today_keywords(self):
        self.assertEqual(self.bounds("yesterday", 1, TZ, today=self.THU),
                         (date(2026, 10, 14), date(2026, 10, 14)))
        self.assertEqual(self.bounds("today", 2, TZ, today=self.THU),
                         (date(2026, 10, 14), date(2026, 10, 15)))

    def test_explicit_date_and_days(self):
        self.assertEqual(self.bounds("2026-10-16", 3, TZ),
                         (date(2026, 10, 14), date(2026, 10, 16)))

    def test_bad_date_mentions_format(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            collect.resolve_period("2026-13-01", 1, TZ)


class TestRender(unittest.TestCase):
    def session(self, users):
        return {"agent": "claude-code", "project": "/p/demo", "id": "i", "start": NOON,
                "end": NOON + timedelta(minutes=5), "users": users, "concl": [],
                "tools": Counter(), "files": set(), "steps": 0}

    def render(self, sessions, **kwargs):
        return collect.render(START, END, {}, [], sessions, False, **kwargs)

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
        sess["id"] = "1b6c7937-f8cf-4e4a-ac46-dd5a5c20381d"
        self.assertIn("· id 5c20381d", self.render([sess]))

    def test_header_carries_generation_time(self):
        out = self.render([], generated=datetime(2026, 10, 18, 19, 5, tzinfo=TZ))
        self.assertIn("生成于 2026-10-18 19:05", out)

    def test_writing_hint_points_to_single_source(self):
        out = self.render([], period_word="周报")
        self.assertIn("写周报前先读 REFERENCE.md", out)
        self.assertIn("SKILL.md", out)


class TestDirtyState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        commit(self.repo, "feat: base", committed="2026-10-01T10:00:00+08:00")
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_repo_has_no_state(self):
        self.assertIsNone(collect.dirty_state(self.repo, self.now))

    def test_modified_tracked_file_is_reported_with_age(self):
        (self.repo / "f.txt").write_text("changed")
        state = collect.dirty_state(self.repo, self.now)
        self.assertEqual((state["repo"], state["files"], state["age_days"]), ("repo", 1, 17))

    def test_untracked_files_are_ignored(self):
        (self.repo / "new.txt").write_text("x")
        self.assertIsNone(collect.dirty_state(self.repo, self.now))

    def test_config_dirty_ignored_for_historical_period(self):
        (self.repo / "f.txt").write_text("changed")
        (states, scanned), err = stderr_of(
            collect.collect_dirty_state, [self.repo], True, False, START, END, self.now)
        self.assertEqual((states, scanned), ([], False))
        self.assertIn("忽略 config.dirty", err)

    def test_explicit_dirty_on_historical_period_warns_but_scans(self):
        (self.repo / "f.txt").write_text("changed")
        (states, scanned), err = stderr_of(
            collect.collect_dirty_state, [self.repo], False, True, START, END, self.now)
        self.assertEqual((len(states), scanned), (1, True))
        self.assertIn("不代表所选历史", err)

    def test_config_dirty_scans_when_period_contains_now(self):
        (self.repo / "f.txt").write_text("changed")
        today = datetime(2026, 10, 18, tzinfo=TZ)
        (states, scanned), _ = stderr_of(
            collect.collect_dirty_state, [self.repo], True, False,
            today, today + timedelta(days=1), self.now)
        self.assertEqual((len(states), scanned), (1, True))


class TestValidateConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(collect.validate_config(collect.load_config(Path("/nonexistent"))), [])

    def test_rejects_bad_values(self):
        cfg = collect.load_config(Path("/nonexistent"))
        cfg.update(author_emails="a@b.c", utc_offset_hours=99, max_sessions=0,
                   dirty="yes", reports_dir="")
        cfg["sources"] = {"git": "yes"}
        joined = "；".join(collect.validate_config(cfg))
        for key in ("author_emails", "utc_offset_hours", "max_sessions", "dirty",
                    "reports_dir", "sources"):
            self.assertIn(key, joined)


class TestShortId(unittest.TestCase):
    UUID = "0199aaaa-b74b-7172-9bf3-05172815cd55"

    def test_claude_id_is_a_bare_uuid(self):
        self.assertEqual(collect.short_id(self.UUID), "2815cd55")

    def test_codex_id_embeds_uuid_after_timestamp(self):
        self.assertEqual(collect.short_id(f"rollout-2026-03-20T10-54-47-{self.UUID}"), "2815cd55")

    def test_dsh_id_embeds_uuid_after_prefix(self):
        self.assertEqual(collect.short_id(f"session-{self.UUID}"), "2815cd55")

    def test_uuid_v7_sessions_started_close_together_do_not_collide(self):
        a = "019d092a-b74b-7172-9bf3-05172815cd55"
        b = "019d092a-c001-7000-8000-0000deadbeef"  # same timestamp prefix
        self.assertNotEqual(collect.short_id(a), collect.short_id(b))

    def test_non_uuid_id_falls_back_to_tail(self):
        self.assertEqual(collect.short_id("abcdef0123456789"), "23456789")
        self.assertEqual(collect.short_id("i"), "i")


if __name__ == "__main__":
    unittest.main(verbosity=2)
