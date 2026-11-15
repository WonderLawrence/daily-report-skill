"""End to end CLI behaviour: argument errors, delivery, redaction, retention."""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import (cli, discovery, gitlog, make_commit, make_repo, commit, git, pipeline,
                     pr_record, run)


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

    def scan_counts(self, *args):
        """The local scan only feeds diagnostics: return (own commits, commits without PR)."""
        r = run(["--date", "2026-10-16", "--no-sessions", "--no-prs", "--print", *args],
                env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        found = re.search(r"本地扫描到本人提交 (\d+)，其中未在本次 PR 检索结果中找到关联 (\d+)", r.stdout)
        return (int(found.group(1)), int(found.group(2))) if found else (0, 0)

    def test_author_filter_drops_other_authors(self):
        repo = make_repo(self.base, "teamrepo")
        commit(repo, "feat: mine", committed="2026-10-16T10:00:00+08:00")
        git(repo, "config", "user.name", "Colleague")
        git(repo, "config", "user.email", "colleague@example.com")
        commit(repo, "feat: theirs", committed="2026-10-16T11:00:00+08:00")
        self.assertEqual(self.scan_counts("--repo", str(repo)), (1, 1),
                         "a teammate's commit must not be counted as the user's")


    def test_global_email_with_regex_characters_matches_literally(self):
        cfg = json.loads(self.cfg.read_text())
        cfg["author_emails"] = []
        self.cfg.write_text(json.dumps(cfg))
        (self.base / ".gitconfig").write_text(
            "[user]\n\tname = Test User\n\temail = dev+work@example.com\n")
        repo = make_repo(self.base, "literalemail", email="dev+work@example.com")
        commit(repo, "feat: mine", committed="2026-10-16T10:00:00+08:00")
        git(repo, "config", "user.name", "Colleague")
        git(repo, "config", "user.email", "devwork@exampleXcom")
        commit(repo, "feat: theirs", committed="2026-10-16T11:00:00+08:00")
        self.assertEqual(self.scan_counts()[0], 1, "a + in the address is matched literally")

    def test_author_regex_overrides_email_config(self):
        repo = make_repo(self.base, "authorregex")
        commit(repo, "feat: mine", committed="2026-10-16T10:00:00+08:00")
        git(repo, "config", "user.name", "Colleague")
        git(repo, "config", "user.email", "colleague@example.com")
        commit(repo, "feat: theirs", committed="2026-10-16T11:00:00+08:00")
        self.assertEqual(self.scan_counts("--author", r"^Colleague <.*@example\.com>$")[0], 1)


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

    def test_cherry_pick_copies_are_collapsed_end_to_end(self):
        """Two copies of one change inside a PR collapse through the real patch id path."""
        repo = make_repo(self.base, "dups")
        commit(repo, "init", committed="2026-10-01T10:00:00+08:00", filename="base.txt")
        git(repo, "checkout", "-q", "-b", "topic")
        commit(repo, "fix(pool): demo", committed="2026-10-16T10:00:00+08:00")
        git(repo, "checkout", "-q", "-b", "copy", "HEAD~1")
        git(repo, "cherry-pick", "topic", env={"GIT_COMMITTER_DATE": "2026-10-16T11:00:00+08:00"})
        shas = git(repo, "log", "--all", "--format=%H", "-2").stdout.split()
        commits = [make_commit("fix(pool): demo", repo="dups", repo_path=str(repo),
                               full_sha=sha, sha=sha[:7], files=["f.txt"],
                               at="2026-10-16T1%d:00:00+08:00" % i)
                   for i, sha in enumerate(shas)]
        out = self.base / "evidence.md"
        argv = ["collect.py", "--config", str(self.cfg), "--date", "2026-10-16",
                "--no-sessions", "--out", str(out)]
        with patch.object(sys, "argv", argv), \
             patch.object(pipeline, "discover_pull_requests",
                          return_value=discovery([pr_record(commits=commits)], commits)), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(), 0)
        text = out.read_text()
        self.assertIn("已折叠 1 个 rebase 副本", text)
        self.assertIn("折叠了 1 个 patch-id 相同", err.getvalue())

    def test_evidence_is_redacted_by_default(self):
        """Commit evidence comes from PRs and is still redacted before being written."""
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        found = [make_commit(f"fix: rotate {secret}", full_sha="a" * 40, sha="a" * 7)]
        out = self.base / "redacted.md"
        argv = ["collect.py", "--config", str(self.cfg), "--date", "2026-10-16",
                "--no-sessions", "--out", str(out)]
        with patch.object(sys, "argv", argv), \
             patch.object(pipeline, "discover_pull_requests",
                          return_value=discovery([pr_record(commits=found)], found)), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        text = out.read_text()
        self.assertIn("[REDACTED:github-token]", text)
        self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
