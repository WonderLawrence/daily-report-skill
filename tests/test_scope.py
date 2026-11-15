"""Strict project scope is shared by commits, fetch, sessions and PR discovery."""
import contextlib
import io
import json
import re
import sys
from unittest.mock import patch

from helpers import (cli, discovery, make_commit, pipeline, pr_record, sessions,
                     SessionFixture, make_repo, commit, write_jsonl, run)


class TestStrictProjectScope(SessionFixture):
    def setUp(self):
        super().setUp()
        self.root = self.home / 'repos'
        self.first = make_repo(self.root, 'same')
        self.second = make_repo(self.root, 'same-other')
        for repo in (self.first, self.second):
            commit(repo, f'feat: {repo.name}', committed='2026-10-16T10:00:00+08:00')
        for name, cwd in [('inside', str(self.first / 'src')), ('outside', str(self.second)),
                          ('unknown', None)]:
            row = {'timestamp': '2026-10-16T12:00:00+08:00', 'type': 'user',
                   'message': {'content': f'处理 {name} 的工作'}}
            if cwd:
                row['cwd'] = cwd
            folder = '-unidentified' if cwd is None else '-fixture'
            write_jsonl(self.home / f'.claude/projects/{folder}/{name}.jsonl', [row])
        self.cfg = self.home / 'config.json'
        self.cfg.write_text(json.dumps({'author_emails': ['dev@example.com'], 'repos_root': str(self.root),
            'reports_dir': str(self.home / 'reports'), 'sources': {'codex': False, 'dsh': False}}))

    def test_only_repo_limits_all_sources_before_fetch_and_pr_discovery(self):
        output = self.home / 'evidence.md'
        found = [make_commit('feat: same', repo='same', repo_path=str(self.first.resolve()),
                             full_sha='a' * 40, sha='a' * 7)]
        with patch.object(sys, 'argv', ['collect.py', '--config', str(self.cfg), '--date', '2026-10-16',
                                       '--only-repo', str(self.first), '--out', str(output)]), \
             patch.object(pipeline, 'fetch_repos', return_value=[]) as fetch, \
             patch.object(pipeline, 'discover_pull_requests',
                          return_value=discovery([pr_record(commits=found)], found)) as prs, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(fetch.call_args.args[0], [self.first.resolve()])
        # Discovery receives the scoped index only, and is told the scope is strict
        self.assertEqual(list(prs.call_args.args[2]), [])
        self.assertTrue(prs.call_args.kwargs['scoped'])
        snapshot = next(self.home.glob('.history/*/*/evidence.json'))
        evidence = json.loads(snapshot.read_text())
        self.assertEqual([c['repo'] for c in evidence['commits']], ['same'])
        self.assertEqual([s['id'] for s in evidence['sessions']], ['inside'])
        self.assertEqual(evidence['diagnostics']['claude']['scope_excluded'], 1)
        self.assertEqual(evidence['diagnostics']['claude']['scope_unknown'], 1)

    def test_config_scope_still_filters_sessions_when_git_disabled(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['only_repos'] = [str(self.first)]
        self.cfg.write_text(json.dumps(cfg))
        result = run(['--config', str(self.cfg), '--date', '2026-10-16', '--no-git', '--print'],
                     env={'HOME': str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('id `inside`', result.stdout)
        self.assertNotIn('id `outside`', result.stdout)
        self.assertNotIn('id `unknown`', result.stdout)

    def scanned(self, *args):
        """How many own commits the diagnostics only local scan covered."""
        result = run(['--config', str(self.cfg), '--date', '2026-10-16', '--no-fetch',
                      '--no-sessions', '--print', *args], env={'HOME': str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        found = re.search(r'本地扫描到本人提交 (\d+)', result.stdout)
        return int(found.group(1)) if found else 0

    def test_repo_remains_additive(self):
        self.assertEqual(self.scanned('--repo', str(self.first)), 2,
                         '--repo only appends; the default root is still scanned')

    def test_cli_scope_overrides_config_and_rejects_conflicting_addition(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['only_repos'] = [str(self.second)]
        self.cfg.write_text(json.dumps(cfg))
        self.assertEqual(self.scanned('--only-repo', str(self.first)), 1,
                         'the CLI scope overrides the configured one')
        conflict = run(['--config', str(self.cfg), '--repo', str(self.first)])
        self.assertEqual(conflict.returncode, 2)
        invalid = run(['--config', str(self.cfg), '--only-repo', str(self.home / 'missing')])
        self.assertEqual(invalid.returncode, 2)

    def test_path_match_does_not_use_basename_and_resolves_alias(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.first, target_is_directory=True)
        self.assertTrue(sessions.project_in_scope(str(alias / 'src'), [self.first.resolve()]))
        self.assertFalse(sessions.project_in_scope(str(self.second), [self.first.resolve()]))
        self.assertIsNone(sessions.project_in_scope('same', [self.first.resolve()]))
