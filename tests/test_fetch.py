"""Fetch integration tests use a local bare remote: no network, no user repositories.

Fetch exists to bring remote commits into the local repository, so that the SHAs returned
by PR discovery can be resolved into changed files and line counts.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import (cli, config, discovery, gitlog, make_commit, pipeline, pr_record, run,
                     git, make_repo, commit, stderr_of, GitDiagnostics)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspaces = self.base / "workspaces"
        self.workspaces.mkdir()
        self.writer = make_repo(self.base, "writer")
        commit(self.writer, "feat: local base", committed="2026-10-16T09:00:00+08:00")
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True,
                       capture_output=True)
        git(self.writer, "remote", "add", "origin", str(self.remote))
        git(self.writer, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.repo = self.workspaces / "reader"
        subprocess.run(["git", "clone", "-q", "--branch", "main", str(self.remote), str(self.repo)],
                       check=True, capture_output=True)
        self.original_head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        commit(self.writer, "fix: remote-only work", committed="2026-10-16T11:00:00+08:00")
        git(self.writer, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.remote_head = git(self.writer, "rev-parse", "HEAD").stdout.strip()
        self.cfg = self.base / "config.json"
        self.cfg.write_text(json.dumps({
            "author_emails": ["dev@example.com"], "repos_root": str(self.workspaces),
            "reports_dir": str(self.base / "reports"),
            "sources": {"git": True, "claude": False, "codex": False, "dsh": False},
        }))
        self.env = {"HOME": str(self.base), "DAILY_REPORT_CONFIG": str(self.cfg)}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, discovered=()):
        """Run the CLI in process so PR discovery can be injected."""
        argv = ["collect.py", "--config", str(self.cfg), "--date", "2026-10-16", *args]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, self.env), \
             patch.object(pipeline, "discover_pull_requests",
                          return_value=discovery([pr_record(commits=discovered)], discovered)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main()

    def shell(self, *args):
        return run(["--date", "2026-10-16", *args], env=self.env)

    def resolve(self, sha):
        """Pretend PR discovery returned a remote commit and see whether fetch makes it local."""
        found = make_commit("fix: remote-only work", repo="reader", repo_path=str(self.repo),
                            full_sha=sha, sha=sha[:7], files=None, added=None, deleted=None)
        return found

    def test_default_fetch_collects_remote_commit_without_touching_worktree(self):
        (self.repo / "f.txt").write_text("unstaged changes")
        (self.repo / "staged.txt").write_text("staged changes")
        git(self.repo, "add", "staged.txt")
        (self.repo / "untracked.txt").write_text("untracked changes")
        status = git(self.repo, "status", "--porcelain").stdout
        found = [self.resolve(self.remote_head)]
        result = self.cli(discovered=found)
        self.assertEqual(result, 0)
        evidence = self.base / "reports/.evidence/20261016.md"
        self.assertIn("fix: remote-only work", evidence.read_text())
        self.assertIn("fetch 成功", evidence.read_text())
        self.assertTrue(found[0].local_available, "after fetch the remote commit resolves")
        self.assertEqual(found[0].files, ["f.txt"])
        self.assertEqual(git(self.repo, "rev-parse", "origin/main").stdout.strip(), self.remote_head)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.original_head)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, status)
        self.assertEqual((self.repo / "f.txt").read_text(), "unstaged changes")
        self.assertEqual((self.repo / "staged.txt").read_text(), "staged changes")
        self.assertEqual((self.repo / "untracked.txt").read_text(), "untracked changes")
        snapshot = next((evidence.parent / ".history").glob("*/*/evidence.json"))
        data = json.loads(snapshot.read_text())
        self.assertEqual(data["git_fetch"][0]["status"], "ok")
        self.assertEqual(data["git_fetch"][0]["remotes"], ["origin"])
        self.assertEqual(data["diagnostics"]["git"]["fetch_ok"], 1)

    def refs(self, remote="origin"):
        return git(self.repo, "for-each-ref", f"refs/remotes/{remote}",
                   "--format=%(refname) %(objectname)").stdout.strip()

    def head(self, remote="origin/main"):
        return git(self.repo, "rev-parse", remote).stdout.strip()

    def test_every_switch_that_decides_whether_refs_move(self):
        """One table for the fetch switches: each row says whether origin/main advances.

        Config can disable fetch and --fetch re-enables it; --no-fetch, a disabled Git
        source and --whoami all leave the refs alone.
        """
        cases = [
            (("--print",), {}, True, "fetch on by default"),
            (("--no-fetch", "--print"), {}, False, "--no-fetch stays offline"),
            (("--print",), {"fetch": False}, False, "config can disable fetch"),
            (("--fetch", "--print"), {"fetch": False}, True, "--fetch overrides the config"),
            (("--no-git", "--print"), {}, False, "no Git source, nothing to fetch"),
            (("--whoami",), {}, False, "the identity check never fetches"),
            (("--print",), {"sources": {"git": False}}, False, "Git source off in config"),
        ]
        base = json.loads(self.cfg.read_text())
        for flags, overrides, advances, reason in cases:
            with self.subTest(reason=reason):
                git(self.repo, "update-ref", "refs/remotes/origin/main", self.original_head)
                self.cfg.write_text(json.dumps({**base, **overrides}))
                result = self.shell(*flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.head(),
                                 self.remote_head if advances else self.original_head, reason)
                # The pack always carries a "fetch 成功也不证明…" caveat, so match the
                # per repository status line instead of the bare phrase
                self.assertEqual("fetch 成功，已按远程配置更新引用" in result.stdout, advances,
                                 reason)

    def test_all_configured_remotes_are_fetched(self):
        upstream = make_repo(self.base, "upstream")
        commit(upstream, "feat: upstream-only work", committed="2026-10-16T12:00:00+08:00")
        git(self.repo, "remote", "add", "upstream", str(upstream))
        result = self.shell("--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(), self.remote_head)
        self.assertTrue(self.refs("upstream"), "the second remote is fetched too")



    def test_fetch_failure_keeps_collecting_and_records_the_gap(self):
        git(self.repo, "remote", "set-url", "origin", str(self.base / "missing.git"))
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[warn] reader: fetch 失败", result.stderr)
        evidence = self.base / "reports/.evidence/20261016.md"
        self.assertIn("远程同步不完整", evidence.read_text())
        snapshot = next((evidence.parent / ".history").glob("*/*/evidence.json"))
        data = json.loads(snapshot.read_text())
        self.assertEqual(data["git_fetch"][0]["status"], "failed")
        self.assertIn("does not appear to be a git repository", data["git_fetch"][0]["detail"])
        self.assertEqual(data["diagnostics"]["git"]["fetch_failed"], 1)
        self.assertEqual(data["diagnostics"]["git"].get("failed_files", 0), 0)

    def test_unfetched_commit_cannot_be_resolved_locally(self):
        """Without fetch the SHA is absent locally, leaving platform metadata only."""
        found = [self.resolve(self.remote_head)]
        self.cli("--no-fetch", discovered=found)
        self.assertFalse(found[0].local_available)
        self.assertIsNone(found[0].files)

    def test_fetch_error_is_sanitized_before_stderr_and_artifact_output(self):
        token = 'ghp_' + 'a' * 36
        secret = 'fixture-url-password'
        error = (f"fatal: could not read https://name:{secret}@example.com/repo.git\n"
                 f"error: ssh://name:{secret}@example.com/repo.git denied\n"
                 f"error: git@example.com:repo.git denied {token} token=fixture-secret-value\n")
        responses = [subprocess.CompletedProcess([], 0, 'origin\n', ''),
                     subprocess.CompletedProcess([], 1, '', error)]
        with patch.object(gitlog, '_git', side_effect=responses):
            results, warning = stderr_of(gitlog.fetch_repos, [self.repo], True, 2, GitDiagnostics())
        for output in (results[0].detail, warning):
            self.assertIn('denied', output)
            self.assertIn('[remote-url]', output)
            for sensitive in (token, secret, 'fixture-secret-value', 'git@example.com:repo.git'):
                self.assertNotIn(sensitive, output)

    def test_no_remote_is_a_normal_local_repository(self):
        git(self.repo, "remote", "remove", "origin")
        result = self.shell("--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("未配置远程", result.stdout)
        self.assertNotIn("[warn]", result.stderr)


    def test_timeout_is_bounded_and_falls_back(self):
        real_run = subprocess.run
        calls = []

        def timeout_fetch(args, **kwargs):
            if "fetch" in args:
                calls.append(kwargs)
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return real_run(args, **kwargs)

        diag = GitDiagnostics()
        with patch.object(gitlog.subprocess, "run", side_effect=timeout_fetch):
            result, err = stderr_of(gitlog.fetch_repos, [self.repo], True, 2, diag)
        self.assertEqual(result[0].status, "timeout")
        self.assertEqual(diag.fetch_failed, 1)
        self.assertIn("继续读取本地记录", err)
        self.assertEqual(calls[0]["timeout"], 2)
        self.assertEqual(calls[0]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(git(self.repo, "rev-parse", "origin/main").stdout.strip(), self.original_head)

    def test_invalid_timeout_is_rejected_before_fetch(self):
        result = self.shell("--fetch-timeout", "0", "--print")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--fetch-timeout", result.stderr)
        self.assertEqual(git(self.repo, "rev-parse", "origin/main").stdout.strip(), self.original_head)
        cfg = config.load_config(self.cfg)
        cfg.update(fetch="yes", fetch_timeout_seconds=0)
        errors = config.validate_config(cfg)
        self.assertTrue(any("fetch 必须" in error for error in errors))
        self.assertTrue(any("fetch_timeout_seconds" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
