#!/usr/bin/env python3
"""collect.py 的回归测试（只用标准库 unittest）。

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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


def make_repo(base: Path, name="repo", user="WangLang", email="wonder_lawrence@163.com") -> Path:
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
        cs = [{"subject": "refactor(txpool): a"}, {"subject": "fix(txpool): b"},
              {"subject": "feat(cli): c"}, {"subject": "chore: d"}]
        rows = collect.scope_summary(cs)
        top = rows[0]
        self.assertEqual(top[0], "txpool")
        self.assertEqual(top[1], 2)

    def test_redact_masks_secrets(self):
        raw = ("ghp_abcdefghijklmnopqrstuvwxyz0123456789 sk-ABCDEFGHIJKLMNOPQRST "
               "10.1.2.3 password=hunter2secret AKIAABCDEFGHIJKLMNOP")
        out = collect.redact_text(raw)
        for leak in ("ghp_", "sk-ABC", "10.1.2.3", "hunter2secret", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(leak, out)

    def test_link_sessions_by_relative_path(self):
        cm = {"repo": "mega-reth", "subject": "refactor(txpool): x",
              "files": ["crates/txpool/src/pool.rs"], "full_sha": "s1", "sha": "s1"}
        sess = {"project": "/Users/u/Project/mega-reth", "files":
                {"/Users/u/Project/mega-reth/crates/txpool/src/pool.rs"}}
        collect.link_sessions([sess], [cm])
        self.assertEqual(len(sess["related"]), 1)
        self.assertEqual(sess["related"][0]["sha"], "s1")

    def test_link_sessions_ignores_other_repo(self):
        cm = {"repo": "mega-reth", "subject": "x", "files": ["a.rs"],
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
            "[user]\n\tname = WangLang\n\temail = wonder_lawrence@163.com\n")
        self.cfg = self.base / "cfg.json"
        self.cfg.write_text(json.dumps({
            "author_emails": ["wonder_lawrence@163.com"],
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
        repo = make_repo(self.base, "aliasrepo", user="WangLang", email="alt@example.com")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
