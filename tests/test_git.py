"""Git evidence: commit reading, author filtering, duplicates, reverts, worktree state."""
import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from helpers import (analysis, evidence, gitlog, make_commit, make_session, make_repo,
                     commit, git, period_of, stderr_of, TZ, PERIOD)


class TestDedupeAndRevert(unittest.TestCase):
    def test_unverifiable_duplicates_are_kept_apart(self):
        """Without a local patch id, keep commits apart: equal title and files prove nothing."""
        base = make_commit(added=10, deleted=1)
        dup = make_commit(added=10, deleted=1, full_sha="2", sha="2")
        shown, extras = analysis.collapse_duplicates([base, dup])
        self.assertEqual((len(shown), extras), (2, 0))

    def test_keeps_distinct_commits(self):
        a = make_commit()
        b = make_commit("feat(x): b", full_sha="2", sha="2")
        shown, extras = analysis.collapse_duplicates([a, b])
        self.assertEqual((len(shown), extras), (2, 0))

    def test_revert_pairing_is_bidirectional(self):
        feat = make_commit("feat: x", full_sha="aa", sha="aa")
        rev = make_commit('Revert "feat: x"', full_sha="bb", sha="bb")
        analysis.pair_reverts([feat, rev])
        self.assertEqual(feat.reverted_by, "bb")
        self.assertEqual(rev.reverts_sha, "aa")

class TestAnalysis(unittest.TestCase):
    def test_scope_summary(self):
        rows = analysis.scope_summary([make_commit(subject) for subject in (
            "refactor(mempool): a", "fix(mempool): b", "feat(cli): c", "chore: d")])
        top = rows[0]
        self.assertEqual(top[0], "mempool")
        self.assertEqual(top[1], 2)

    def test_redact_masks_secrets(self):
        raw = ("ghp_abcdefghijklmnopqrstuvwxyz0123456789 sk-ABCDEFGHIJKLMNOPQRST "
               "10.1.2.3 password=hunter2secret AKIAABCDEFGHIJKLMNOP")
        out = evidence.redact_text(raw)
        for leak in ("ghp_", "sk-ABC", "10.1.2.3", "hunter2secret", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(leak, out)

    def test_link_sessions_by_relative_path(self):
        cm = make_commit("refactor(mempool): x", repo="demo-node", full_sha="s1", sha="s1",
                         files=["crates/mempool/src/pool.rs"])
        sess = make_session(project="/Users/u/Project/demo-node",
                            files={"/Users/u/Project/demo-node/crates/mempool/src/pool.rs"})
        analysis.link_sessions([sess], [cm])
        self.assertEqual(len(sess.related), 1)
        self.assertEqual(sess.related[0].sha, "s1")

    def test_link_sessions_ignores_other_repo(self):
        cm = make_commit("x", repo="demo-node", full_sha="s1", sha="s1")
        sess = make_session(project="/Users/u/Project/other",
                            files={"/Users/u/Project/other/a.rs"})
        analysis.link_sessions([sess], [cm])
        self.assertEqual(sess.related, [])

    def test_mark_near_duplicates(self):
        s1 = make_session(project="/p", users=["same prompt"],
                          start=datetime(2026, 10, 12, tzinfo=TZ))
        s2 = make_session(project="/p", users=["same prompt"],
                          start=datetime(2026, 10, 13, tzinfo=TZ))
        analysis.mark_near_duplicates([s1, s2])
        self.assertEqual((s1.dup_n, s2.dup_n), (2, 2))

    def test_prune_evidence_keeps_newest(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for i in range(5):
                p = d / f"{i}.md"
                p.write_text("x")
                os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
            evidence.prune_evidence(d, 2)
            self.assertEqual(sorted(p.name for p in d.glob("*.md")), ["3.md", "4.md"])

class TestAuthorMatching(unittest.TestCase):
    def test_configured_emails_match_in_full_only(self):
        """The regex built from author_emails must match whole emails, dots included."""
        single = gitlog.author_regex(None, ["dev@example.com"])
        both = gitlog.author_regex(None, ["dev@example.com", "alt@example.com"])
        cases = [
            (single, "Test User <dev@example.com>", True),
            (single, "Colleague <dev@example.com.evil>", False),   # suffix must not match
            (single, "Colleague <otherdev@example.com>", False),   # prefix must not match
            (single, "Colleague <dev@exampleXcom>", False),        # the dot is literal
            (single, "dev@example.com <colleague@example.com>", False),  # name is not the email
            (single, "Alternate <alt@example.com>", False),
            (both, "Alternate <alt@example.com>", True),           # alternation of identities
        ]
        for pattern, who, mine in cases:
            with self.subTest(author=who):
                self.assertEqual(bool(re.search(pattern, who, re.I)), mine)

    def test_an_explicit_regex_replaces_the_email_list(self):
        pattern = gitlog.author_regex(r"^Colleague <", ["dev@example.com"])
        self.assertTrue(re.search(pattern, "Colleague <anyone@example.com>"))
        self.assertFalse(re.search(pattern, "Test User <dev@example.com>"))


class TestGitCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_committer_date_not_author_date(self):
        repo = make_repo(self.base)
        # Authored two weeks ago, landed today
        commit(repo, "refactor(x): landed today", authored="2026-09-28T10:00:00+08:00",
               committed="2026-10-16T10:00:00+08:00")
        start = datetime(2026, 10, 16, tzinfo=TZ)
        got = gitlog.git_commits(repo, period_of(start, start + timedelta(days=1)))
        self.assertEqual(len(got), 1, "attributed by the date it landed")
        self.assertTrue(got[0].authored.startswith("2026-09-28"))
        self.assertTrue(got[0].at.startswith("2026-10-16"))

    def test_excludes_stash(self):
        repo = make_repo(self.base)
        commit(repo, "feat: base", committed="2026-10-16T10:00:00+08:00")
        (repo / "f.txt").write_text("dirty")
        git(repo, "stash", "push", "-q", "-m", "wip")
        start = datetime(2026, 10, 16, tzinfo=TZ)
        got = gitlog.git_commits(repo, period_of(start, start + timedelta(days=2)))
        self.assertFalse([c for c in got if c.subject.startswith(("index on", "untracked files"))])

    def test_collapses_real_cherry_pick_copy(self):
        """Real commits go through git patch-id, which once crashed on bytes/text mixing."""
        repo = make_repo(self.base)
        commit(repo, "init", committed="2026-10-01T10:00:00+08:00", filename="base.txt")
        git(repo, "checkout", "-q", "-b", "topic")
        commit(repo, "fix(pool): demo", committed="2026-10-16T10:00:00+08:00")
        git(repo, "checkout", "-q", "-b", "copy", "HEAD~1")
        git(repo, "cherry-pick", "topic", env={"GIT_COMMITTER_DATE": "2026-10-16T11:00:00+08:00"})
        start = datetime(2026, 10, 16, tzinfo=TZ)
        commits = gitlog.git_commits(repo, period_of(start, start + timedelta(days=1)))
        self.assertEqual(len(commits), 2)
        self.assertIsNotNone(gitlog.git_patch_id(repo, commits[0].full_sha))
        shown, extras = analysis.collapse_duplicates(commits)
        self.assertEqual((len(shown), extras), (1, 1))

    def test_week_window_includes_only_committer_range(self):
        repo = make_repo(self.base)
        commit(repo, "feat: in", committed="2026-10-15T10:00:00+08:00")
        commit(repo, "feat: out", committed="2026-10-01T10:00:00+08:00")
        start = datetime(2026, 10, 12, tzinfo=TZ)
        got = gitlog.git_commits(repo, period_of(start, start + timedelta(days=7)))
        self.assertEqual([c.subject for c in got], ["feat: in"])

class TestDirtyState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        commit(self.repo, "feat: base", committed="2026-10-01T10:00:00+08:00")
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_repo_has_no_state(self):
        self.assertIsNone(gitlog.dirty_state(self.repo, self.now))

    def test_modified_tracked_file_is_reported_with_age(self):
        (self.repo / "f.txt").write_text("changed")
        state = gitlog.dirty_state(self.repo, self.now)
        self.assertEqual((state.repo, state.files, state.age_days), ("repo", 1, 17))

    def test_untracked_files_are_ignored(self):
        (self.repo / "new.txt").write_text("x")
        self.assertIsNone(gitlog.dirty_state(self.repo, self.now))

    def test_config_dirty_ignored_for_historical_period(self):
        (self.repo / "f.txt").write_text("changed")
        (states, scanned), err = stderr_of(
            gitlog.collect_dirty_state, [self.repo], True, False, PERIOD, self.now)
        self.assertEqual((states, scanned), ([], False))
        self.assertIn("忽略 config.dirty", err)

    def test_explicit_dirty_on_historical_period_warns_but_scans(self):
        (self.repo / "f.txt").write_text("changed")
        (states, scanned), err = stderr_of(
            gitlog.collect_dirty_state, [self.repo], False, True, PERIOD, self.now)
        self.assertEqual((len(states), scanned), (1, True))
        self.assertIn("不代表所选历史", err)

    def test_config_dirty_scans_when_period_contains_now(self):
        (self.repo / "f.txt").write_text("changed")
        today = datetime(2026, 10, 18, tzinfo=TZ)
        (states, scanned), _ = stderr_of(
            gitlog.collect_dirty_state, [self.repo], True, False,
            period_of(today, today + timedelta(days=1)), self.now)
        self.assertEqual((len(states), scanned), (1, True))


if __name__ == "__main__":
    unittest.main()
