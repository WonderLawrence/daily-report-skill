"""Strict project scope is shared by commits, fetch, sessions and PR discovery."""
import contextlib
import io
import json
import re
import sys
from unittest.mock import patch

from helpers import (cli, discovery, make_commit, pipeline, pr_record, sessions,
                     SessionFixture, make_repo, commit, write_jsonl, run)
from daily_report import settings


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

    def test_target_project_limits_all_sources_before_fetch_and_pr_discovery(self):
        output = self.home / 'evidence.md'
        found = [make_commit('feat: same', repo='same', repo_path=str(self.first.resolve()),
                             full_sha='a' * 40, sha='a' * 7)]
        with patch.object(sys, 'argv', ['collect.py', '--config', str(self.cfg), '--date', '2026-10-16',
                                       '--target-project', str(self.first), '--out', str(output)]), \
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
        self.assertEqual(evidence['scope']['target_projects'], [str(self.first.resolve())])
        self.assertNotIn('only_repos', evidence['scope'])
        self.assertEqual(evidence['diagnostics']['claude']['scope_excluded'], 1)
        self.assertEqual(evidence['diagnostics']['claude']['scope_unknown'], 1)

    def test_config_scope_still_filters_sessions_when_git_disabled(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['target_projects'] = [str(self.first)]
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

    def test_repeated_targets_are_combined_and_deduplicated(self):
        self.assertEqual(self.scanned('--target-project', str(self.first),
                                      '--target-project', str(self.second),
                                      '--target-project', str(self.first)), 2)

    def test_empty_targets_use_default_discovery(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['target_projects'] = []
        self.cfg.write_text(json.dumps(cfg))
        self.assertEqual(self.scanned(), 2)

    def test_old_config_name_fails_before_any_discovery_or_fetch(self):
        original = json.loads(self.cfg.read_text())
        for old_targets, new_targets in (([], []), ([str(self.first)], []),
                                         ([str(self.first)], [str(self.second)])):
            with self.subTest(old_targets=old_targets, new_targets=new_targets):
                cfg = {**original, 'only_repos': old_targets, 'target_projects': new_targets}
                self.cfg.write_text(json.dumps(cfg))
                errors = io.StringIO()
                with patch.object(sys, 'argv', ['collect.py', '--config', str(self.cfg), '--print']), \
                     patch.object(settings, 'discover_repos') as scan, \
                     patch.object(pipeline, 'fetch_repos') as fetch, \
                     patch.object(pipeline, 'discover_pull_requests') as prs, \
                     contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as error:
                        cli.main()
                    self.assertEqual(error.exception.code, 2)
                scan.assert_not_called()
                fetch.assert_not_called()
                prs.assert_not_called()
                self.assertIn('only_repos 已更名为 target_projects', errors.getvalue())

    def test_old_cli_name_is_not_an_alias(self):
        result = run(['--config', str(self.cfg), '--only-repo', str(self.first), '--print'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments: --only-repo', result.stderr)

    def test_non_git_project_still_requires_git_collection_disabled(self):
        project = self.home / 'notes'
        project.mkdir()
        write_jsonl(self.home / '.claude/projects/-fixture/notes.jsonl', [{
            'timestamp': '2026-10-16T12:00:00+08:00', 'type': 'user', 'cwd': str(project),
            'message': {'content': '整理设计文档'},
        }])
        args = ['--config', str(self.cfg), '--date', '2026-10-16',
                '--target-project', str(project), '--print']
        rejected = run(args, env={'HOME': str(self.home)})
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('仓库根目录', rejected.stderr)
        accepted = run([*args, '--no-git'], env={'HOME': str(self.home)})
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('id `notes`', accepted.stdout)
        self.assertNotIn('id `inside`', accepted.stdout)

    def test_cli_scope_overrides_config_and_rejects_conflicting_addition(self):
        cfg = json.loads(self.cfg.read_text())
        cfg['target_projects'] = [str(self.second)]
        self.cfg.write_text(json.dumps(cfg))
        self.assertEqual(self.scanned('--target-project', str(self.first)), 1,
                         'the CLI scope overrides the configured one')
        conflict = run(['--config', str(self.cfg), '--repo', str(self.first)])
        self.assertEqual(conflict.returncode, 2)
        invalid = run(['--config', str(self.cfg), '--target-project', str(self.home / 'missing')])
        self.assertEqual(invalid.returncode, 2)

    def test_path_match_does_not_use_basename_and_resolves_alias(self):
        alias = self.home / 'alias'
        alias.symlink_to(self.first, target_is_directory=True)
        self.assertTrue(sessions.project_in_scope(str(alias / 'src'), [self.first.resolve()]))
        self.assertFalse(sessions.project_in_scope(str(self.second), [self.first.resolve()]))
        self.assertIsNone(sessions.project_in_scope('same', [self.first.resolve()]))
