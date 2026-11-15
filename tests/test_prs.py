"""Two level PR traversal and selected PR details, on fake responses and local repos only."""
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from helpers import SessionFixture, make_repo, commit, git, make_identity, run, PERIOD
from daily_report import github as prs
from daily_report import pr_report
from daily_report.evidence import json_evidence
from daily_report.models import (CommitRef, DetailsReport, Involvement, MessageStatus,
                                 PullRequest, Snapshot)

_spec = importlib.util.spec_from_file_location("pr_details_entry",
                                               Path(__file__).resolve().parents[1]
                                               / "scripts" / "pr_details.py")
pr_details = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_details)

LOGIN = "me"


def search_item(repository="acme/node", number=1, title="Fix RPC 1", state="open",
                author="someone-else", updated="2026-10-16T10:00:00Z"):
    return {"number": number, "title": title, "state": state,
            "html_url": f"https://github.com/{repository}/pull/{number}",
            "repository_url": f"https://api.github.com/repos/{repository}",
            "updated_at": updated, "user": {"login": author}}


def api_commit(sha, login=LOGIN, email="dev@example.com", message="fix: RPC null result",
               committed="2026-10-16T02:00:00Z", authored="2026-10-16T01:00:00Z"):
    return {"sha": sha, "author": {"login": login},
            "commit": {"message": message,
                       "author": {"name": "Dev", "email": email, "date": authored},
                       "committer": {"date": committed}}}


class TestPullRequestDiscovery(unittest.TestCase):
    def discover(self, items, commits_by_pr, login=LOGIN, total=None, pages=None,
                 incomplete=False, identity=None, **kwargs):
        def fake_api(endpoint, timeout, paginate=False, item_key=None):
            if endpoint == "user":
                if login is None:
                    raise prs.GitHubError("HTTP 403")
                return {"login": login}
            if endpoint.startswith("search/issues"):
                self.last_search = endpoint
                page = int(parse_qs(urlsplit(endpoint).query)['page'][0])
                return {"total_count": len(items) if total is None else total,
                        "items": pages[page - 1] if pages is not None else items,
                        "incomplete_results": incomplete}
            for number, payload in commits_by_pr.items():
                if f"pulls/{number}/commits" in endpoint:
                    if isinstance(payload, Exception):
                        raise payload
                    return payload
            raise AssertionError(f"unexpected endpoint {endpoint}")

        with patch.object(prs, "api", side_effect=fake_api) as api, \
             patch.object(prs.shutil, "which", return_value="/fixture/gh"):
            self.api = api
            return prs.discover_pull_requests(PERIOD, identity or make_identity(), **kwargs)

    def test_traversal_keeps_own_in_window_commits_and_drops_the_rest(self):
        mine = api_commit("a" * 40)
        theirs = api_commit("b" * 40, login="other", email="other@example.com")
        earlier = api_commit("c" * 40, committed="2026-10-01T02:00:00Z")
        found, commits, diag = self.discover([search_item()], {1: [mine, theirs, earlier]},
                                             repo_index={"acme/node": Path("/local/node")})
        self.assertEqual(diag.status, "ok")
        self.assertEqual([c.full_sha for c in commits], ["a" * 40])
        kept = commits[0]
        self.assertEqual((kept.repo, kept.repo_path), ("node", "/local/node"))
        self.assertEqual(kept.at, "2026-10-16T10:00:00+08:00", "UTC is normalized to the configured zone")
        self.assertEqual(kept.pr_ids, ["acme/node#1"])
        self.assertEqual(found[0].involvement, "commits")
        self.assertEqual(len(found[0].associated_commits), 1)

    def test_contribution_to_someone_elses_pr_is_kept_without_description(self):
        found, commits, _ = self.discover([search_item(author="someone-else")],
                                          {1: [api_commit("a" * 40)]})
        self.assertEqual(found[0].author_login, "someone-else")
        self.assertEqual(len(commits), 1)
        self.assertFalse(hasattr(found[0], "body"))
        self.assertNotIn("description", {f.name for f in dataclasses.fields(found[0])})

    def test_pr_without_own_period_commits_stays_for_screening(self):
        found, commits, diag = self.discover(
            [search_item(number=7, title="Review only")],
            {7: [api_commit("b" * 40, login="other", email="other@example.com")]})
        self.assertEqual(commits, [])
        self.assertEqual(found[0].involvement, "no_period_commits")
        self.assertEqual((diag.prs_with_commits, diag.prs_without_commits), (0, 1))

    def test_same_commit_in_two_prs_is_deduped_and_keeps_both_ids(self):
        shared = api_commit("a" * 40)
        found, commits, _ = self.discover([search_item(number=1), search_item(number=2)],
                                          {1: [shared], 2: [shared]})
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].pr_ids, ["acme/node#1", "acme/node#2"])
        self.assertTrue(all(len(pr.associated_commits) == 1 for pr in found))

    def test_scope_drops_pull_requests_outside_the_selected_repositories(self):
        found, commits, diag = self.discover(
            [search_item(repository="acme/node"), search_item(repository="acme/sdk", number=9)],
            {1: [api_commit("a" * 40)]},
            repo_index={"acme/node": Path("/local/node")}, scoped=True)
        self.assertEqual([pr.id for pr in found], ["acme/node#1"])
        self.assertEqual((diag.out_of_scope, len(commits)), (1, 1))
        self.assertIn('repo:acme/node', parse_qs(urlsplit(self.last_search).query)['q'][0])

    def test_login_lookup_failure_falls_back_to_configured_email(self):
        by_email = api_commit("a" * 40, login=None)
        found, commits, diag = self.discover([search_item()], {1: [by_email]}, login=None)
        self.assertEqual(len(commits), 1)
        self.assertIn("只按配置邮箱匹配", diag.detail)

    def test_query_budget_and_rate_limit_preserve_partial_results(self):
        items = [search_item(number=1), search_item(number=2)]
        found, commits, diag = self.discover(items, {1: [api_commit("a" * 40)]},
                                             max_queries=2)
        self.assertEqual((len(commits), diag.unqueried, diag.status), (1, 1, "partial"))
        self.assertEqual(found[1].involvement, "unqueried")

        blocked = prs.GitHubError("HTTP 403", stop=True)
        found, commits, diag = self.discover(items, {1: blocked, 2: [api_commit("a" * 40)]})
        self.assertEqual((commits, diag.failures, diag.unqueried), ([], 1, 1))
        self.assertEqual(found[0].involvement, "failed")
        self.assertEqual(diag.prs_without_commits, 0, "unknown is not a confirmed empty list")

    def test_search_failure_is_unavailable_and_queries_nothing_else(self):
        def failing(endpoint, timeout, paginate=False, item_key=None):
            if endpoint == "user":
                return {"login": LOGIN}
            raise prs.GitHubError("GitHub 查询失败（HTTP 503）")

        with patch.object(prs, "api", side_effect=failing), \
             patch.object(prs.shutil, "which", return_value="/fixture/gh"):
            found, commits, diag = prs.discover_pull_requests(PERIOD, make_identity())
        self.assertEqual((found, commits, diag.status), ([], [], "unavailable"))
        self.assertIn("503", diag.detail)

    def test_missing_cli_and_disabled_mode_do_not_call_github(self):
        with patch.object(prs, "api") as api:
            with patch.object(prs.shutil, "which", return_value=None):
                self.assertEqual(prs.discover_pull_requests(PERIOD, make_identity())[2].status,
                                 "unavailable")
            self.assertEqual(prs.discover_pull_requests(PERIOD, make_identity(),
                                                        enabled=False)[2].status, "disabled")
        api.assert_not_called()

    def test_search_uses_the_real_login_and_reports_truncation(self):
        _, _, diag = self.discover([search_item()], {1: [api_commit("a" * 40)]}, total=500)
        self.assertIn("updated%3A%3E%3D2026-10-16", self.last_search)
        # @me is web/CLI sugar; the REST search needs the real login to be safe
        self.assertIn(f"involves%3A{LOGIN}", self.last_search)
        self.assertNotIn("%40me", self.last_search)
        self.assertIn("500", diag.detail)
        self.assertEqual(diag.status, "partial")
        self.assertTrue(diag.search_truncated)

    def test_search_continues_past_the_first_hundred(self):
        items = [search_item(number=n) for n in range(1, 102)]
        found, commits, diag = self.discover(
            items, {n: [] for n in range(1, 102)}, pages=[items[:100], items[100:]])
        self.assertEqual(len(found), 101)
        self.assertEqual(diag.status, 'ok')
        self.assertEqual(diag.queries, 103)
        self.assertFalse(diag.search_truncated)

    def test_malformed_search_record_is_a_gap_without_hiding_valid_records(self):
        malformed = search_item(number=['invalid'])
        found, _, diag = self.discover([malformed, search_item()], {1: []})
        self.assertEqual([pr.id for pr in found], ['acme/node#1'])
        self.assertEqual((diag.failures, diag.status), (1, 'partial'))

    def test_incomplete_results_is_partial_even_when_reported_count_matches(self):
        _, _, diag = self.discover([search_item()], {1: []}, incomplete=True)
        self.assertEqual(diag.status, 'partial')
        self.assertTrue(diag.search_incomplete)

    def test_search_budget_stops_paging_and_preserves_candidates(self):
        items = [search_item(number=n) for n in range(1, 101)]
        found, _, diag = self.discover(items, {}, total=101, max_queries=1)
        self.assertEqual(len(found), 100)
        self.assertEqual((diag.status, diag.queries, diag.unqueried), ('partial', 1, 100))
        self.assertTrue(diag.search_truncated)

    def test_platform_search_cap_is_reported(self):
        items = [search_item(number=n) for n in range(1, 1001)]
        pages = [items[i:i + 100] for i in range(0, 1000, 100)]
        found, _, diag = self.discover(items, {}, total=1001, pages=pages, max_queries=10)
        self.assertEqual((len(found), diag.queries, diag.status), (1000, 10, 'partial'))
        self.assertTrue(diag.search_truncated)
        self.assertIn('1001', diag.detail)

    def test_later_page_failure_keeps_first_page_and_marks_gap(self):
        items = [search_item(number=n) for n in range(1, 101)]
        with patch.object(prs, 'viewer_login', return_value=LOGIN), \
             patch.object(prs.shutil, 'which', return_value='/fixture/gh'), \
             patch.object(prs, 'search_pull_requests', side_effect=[
                 (items, 101, False), prs.GitHubError('HTTP 429', stop=True)]):
            found, _, diag = prs.discover_pull_requests(PERIOD, make_identity())
        self.assertEqual((len(found), diag.status, diag.unqueried), (100, 'partial', 100))
        self.assertTrue(diag.search_truncated)

    def test_scoped_search_is_not_crowded_out_by_other_repositories(self):
        def fake_api(endpoint, timeout, paginate=False, item_key=None):
            if endpoint == 'user':
                return {'login': LOGIN}
            if endpoint.startswith('search/issues'):
                query = parse_qs(urlsplit(endpoint).query)['q'][0]
                scoped = 'repo:acme/node' in query
                return {'total_count': 1 if scoped else 101, 'items': [search_item()] if scoped
                        else [search_item('other/repo', n) for n in range(1, 101)]}
            return [api_commit('a' * 40)]
        with patch.object(prs, 'api', side_effect=fake_api), \
             patch.object(prs.shutil, 'which', return_value='/fixture/gh'):
            found, commits, diag = prs.discover_pull_requests(
                PERIOD, make_identity(), {'acme/node': Path('/local/node')}, scoped=True)
        self.assertEqual([pr.id for pr in found], ['acme/node#1'])
        self.assertEqual((len(commits), diag.status), (1, 'ok'))

    def test_multiple_scopes_respect_budget_without_silent_omissions(self):
        _, _, diag = self.discover([], {}, scoped=True, max_queries=1,
                                  repo_index={'acme/node': Path('/node'), 'acme/sdk': Path('/sdk')})
        self.assertEqual((diag.status, diag.searches_skipped), ('partial', 1))

    def test_unknown_strict_repository_does_not_trigger_global_search(self):
        with patch.object(prs, 'api') as api:
            _, _, diag = prs.discover_pull_requests(PERIOD, make_identity(), scoped=True)
        self.assertEqual(diag.status, 'unavailable')
        api.assert_not_called()

    def test_commit_list_cap_warns_without_dropping_readable_commits(self):
        raw = [api_commit(f'{n:040x}') for n in range(250)]
        _, commits, diag = self.discover([search_item()], {1: raw})
        self.assertEqual((len(commits), diag.status, diag.commit_lists_capped), (250, 'partial', 1))

    def test_explicit_author_is_not_broadened_by_login(self):
        identity = dataclasses.replace(make_identity(), author_re='other@example.com', author_explicit=True)
        mine = api_commit('a' * 40)
        selected = api_commit('b' * 40, login='other', email='other@example.com')
        _, commits, _ = self.discover([search_item()], {1: [mine, selected]}, identity=identity)
        self.assertEqual([c.full_sha for c in commits], ['b' * 40])
        self.assertIsNone(identity.login, 'discovery must not mutate caller identity')

    def test_automatic_identity_still_accepts_login_with_another_email(self):
        _, commits, _ = self.discover([search_item()], {
            1: [api_commit('a' * 40, email='123+me@users.noreply.github.com')]})
        self.assertEqual(len(commits), 1)

    def test_search_falls_back_to_at_me_when_login_is_unknown(self):
        self.discover([search_item()], {1: [api_commit("a" * 40)]}, login=None)
        self.assertIn("involves%3A%40me", self.last_search)


class TestGitHubAccess(unittest.TestCase):
    def test_remote_parsing_accepts_github_and_does_not_guess_hosts(self):
        for remote in ('git@github.com:acme/node.git', 'https://github.com/acme/node.git',
                       'ssh://git@github.com/acme/node.git'):
            self.assertEqual(prs.github_repository(remote), 'acme/node')
        for remote in ('git@gitlab.com:acme/node.git', 'git@github-work:acme/node.git',
                       '/tmp/remote.git', 'https://example.com/acme/node',
                       'https://[invalid/acme/node', 'https://github.com/../node'):
            self.assertIsNone(prs.github_repository(remote))

    def test_repository_url_parsing_rejects_unexpected_shapes(self):
        self.assertEqual(prs.repository_from_url("https://api.github.com/repos/acme/node"),
                         "acme/node")
        for value in (None, "", "https://api.github.com/repos/acme", "https://x/repos/../node"):
            self.assertIsNone(prs.repository_from_url(value))

    def test_timeout_aborts_queries_without_storing_raw_exception(self):
        with patch.object(prs.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 1)), \
             self.assertRaises(prs.GitHubError) as error:
            prs.api('repos/acme/node/pulls/1', 1)
        self.assertTrue(error.exception.stop)
        self.assertNotIn('secret', str(error.exception))

    def test_api_is_read_only_paginated_and_hides_raw_errors(self):
        result = subprocess.CompletedProcess([], 0, '[[{"number":1}],[{"number":2}]]', '')
        with patch.object(prs.subprocess, 'run', return_value=result) as run:
            data = prs.api('repos/acme/node/commits/abc/pulls', 2, paginate=True)
        self.assertEqual(len(data), 2)
        self.assertIn('GET', run.call_args.args[0])
        self.assertIn('--paginate', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        result = subprocess.CompletedProcess([], 1, '', 'secret-token (HTTP 401)')
        with patch.object(prs.subprocess, 'run', return_value=result), self.assertRaises(prs.GitHubError) as error:
            prs.api('repos/acme/node/pulls/1', 2)
        self.assertNotIn('secret-token', str(error.exception))
        self.assertTrue(error.exception.stop)

    def test_search_response_must_carry_items(self):
        result = subprocess.CompletedProcess([], 0, '[{"number":1}]', '')
        with patch.object(prs.subprocess, 'run', return_value=result), \
             self.assertRaises(prs.GitHubError):
            prs.api('search/issues?q=x', 2, item_key="items")


class TestSelectedDetails(SessionFixture):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.home)
        commit(self.repo, 'feat: old work', committed='2026-10-01T09:00:00+08:00', filename='old.txt')
        commit(self.repo, 'fix: RPC null result', authored='2026-10-16T09:00:00+08:00',
               committed='2026-10-16T10:00:00+08:00')
        self.sha = git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
        self.pr = PullRequest(
            id='acme/node#1', repository='acme/node', number=1,
            url='https://github.com/acme/node/pull/1', title='Fix RPC 1',
            state_at_discovery='open', author_login='me',
            updated_at='2026-10-16T10:00:00Z', retrieved_at='2026-10-16T12:00:00+00:00',
            involvement=Involvement.COMMITS,
            associated_commits=[CommitRef(full_sha=self.sha, repo_path=str(self.repo),
                                          at='2026-10-16T10:00:00+08:00',
                                          authored='2026-10-16T09:00:00+08:00',
                                          author='Dev <dev@example.com>',
                                          subject='fix: RPC null result')])

    def details(self, body='Description must not enter discovery', **over):
        payload = dict({'number': 1, 'title': 'Fix RPC 1', 'body': body, 'state': 'open',
                        'head': {'sha': self.sha}}, **over)
        with patch.object(prs, 'api', return_value=payload) as api:
            return prs.selected_details(self.pr), api

    def test_description_and_commit_messages_are_loaded_without_diffs(self):
        details, api = self.details()
        self.assertEqual(api.call_args.args[0], 'repos/acme/node/pulls/1')
        self.assertEqual(details.description, 'Description must not enter discovery')
        entry = details.period_commits[0]
        self.assertEqual(entry.message, 'fix: RPC null result')
        self.assertNotIn('feat: old work', entry.message)
        # The change itself is reviewed on demand and never prefetched
        self.assertNotIn('patch', {f.name for f in dataclasses.fields(entry)})
        self.assertNotIn('diff --git', json.dumps(json_evidence(details, False)))

    def test_snapshot_reading_tolerates_newer_fields_but_not_missing_ones(self):
        """One hydration policy: unknown keys are dropped, a missing required field raises."""
        source = self.home / 'evidence.json'
        serialized = json_evidence(self.pr, False)
        serialized['future_field'] = 1
        serialized['associated_commits'][0]['future_field'] = 2
        source.write_text(json.dumps({'start': '2026-10-16T00:00:00+08:00',
            'end_exclusive': '2026-10-17T00:00:00+08:00', 'pull_requests': [serialized]}))
        snapshot, chosen = pr_report.load_selection(source, ['acme/node#1'])
        self.assertEqual(snapshot.start, '2026-10-16T00:00:00+08:00')
        self.assertEqual(list(chosen), ['acme/node#1'])
        self.assertEqual(chosen['acme/node#1'].associated_commits[0].full_sha, self.sha)

        serialized.pop('title')
        source.write_text(json.dumps({'start': '2026-10-16T00:00:00+08:00',
            'end_exclusive': '2026-10-17T00:00:00+08:00', 'pull_requests': [serialized]}))
        with self.assertRaisesRegex(ValueError, 'title'):
            pr_report.load_selection(source, ['acme/node#1'])

    def test_unreadable_commit_is_a_gap_while_description_is_preserved(self):
        self.pr.associated_commits[0].full_sha = 'f' * 40
        details, _ = self.details()
        self.assertEqual(details.period_commits[0].message_status, MessageStatus.FAILED)
        self.assertEqual(details.description, 'Description must not enter discovery')

    def test_selected_pr_without_commits_loads_background_and_carries_participation_gap(self):
        source = self.home / 'evidence.json'
        self.pr.associated_commits = []
        for involvement in (Involvement.NO_PERIOD_COMMITS, Involvement.FAILED, Involvement.UNQUERIED):
            with self.subTest(involvement=involvement):
                self.pr.involvement = involvement
                source.write_text(json.dumps({'start': PERIOD.start.isoformat(),
                    'end_exclusive': PERIOD.end.isoformat(),
                    'pull_requests': [json_evidence(self.pr, False)]}))
                snapshot, chosen = pr_report.load_selection(source, [self.pr.id])
                with patch.object(prs, 'api', return_value={'title': 'Review PR', 'body': 'Background'}), \
                     patch.object(prs, '_period_commit') as read_commit:
                    details, failures = pr_report.fetch_details(chosen, 30)
                self.assertEqual(failures, [])
                self.assertEqual(details[0].description, 'Background')
                self.assertEqual(details[0].period_commits, [])
                read_commit.assert_not_called()
                limitations = ' '.join(details[0].limitations)
                self.assertIn('评审/评论记录或用户说明', limitations)
                self.assertEqual('提交查询未完成' in limitations, involvement != Involvement.NO_PERIOD_COMMITS)

    def test_cli_batch_keeps_merge_only_pr_and_all_other_selected_prs(self):
        """A merge-only PR in the middle must not reject the entire nine-PR selection."""
        records, responses = [], {}
        merged_at = '2026-10-16T03:00:00Z'
        for number in range(2607, 2616):
            merge_only = number == 2611
            pr = dataclasses.replace(
                self.pr, id=f'acme/node#{number}', number=number,
                url=f'https://github.com/acme/node/pull/{number}',
                title=f'Fixture PR {number}',
                state_at_discovery='closed' if merge_only else 'open',
                involvement=Involvement.NO_PERIOD_COMMITS if merge_only else Involvement.COMMITS,
                associated_commits=[] if merge_only else self.pr.associated_commits)
            records.append(pr)
            responses[f'repos/acme/node/pulls/{number}'] = {
                'title': pr.title, 'body': f'Fixture description {number}',
                'state': pr.state_at_discovery, 'head': {'sha': self.sha},
                'merged_at': merged_at if merge_only else None,
            }
        source = self.home / 'evidence.json'
        source.write_text(json.dumps({'start': PERIOD.start.isoformat(),
            'end_exclusive': PERIOD.end.isoformat(), 'pull_requests': json_evidence(records, False)}))
        stub = self.home / 'bin/gh'
        stub.parent.mkdir()
        stub.with_suffix('.json').write_text(json.dumps(responses))
        stub.write_text('#!/usr/bin/env python3\n'
                        'import json, sys\n'
                        'from pathlib import Path\n'
                        'responses = json.loads(Path(__file__).with_suffix(".json").read_text())\n'
                        'print(json.dumps(responses[sys.argv[-1]]))\n')
        stub.chmod(0o755)
        output = self.home / 'selected.md'
        args = [sys.executable, pr_details.__file__, '--evidence', str(source), '--out', str(output)]
        for pr in records:
            args += ['--pr', pr.id]
        result = subprocess.run(args, capture_output=True, text=True,
                                env=dict(os.environ, PATH=f'{stub.parent}{os.pathsep}{os.environ["PATH"]}'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[ok] 9 PR details', result.stdout)
        snapshot = next(self.home.glob('.history/selected.md/*/evidence.json'))
        data = json.loads(snapshot.read_text())
        self.assertEqual(data['selected_prs'], [pr.id for pr in records])
        self.assertEqual(data['failures'], [])
        by_id = {pr['id']: pr for pr in data['pull_requests']}
        self.assertEqual(set(by_id), {pr.id for pr in records})
        merged = by_id['acme/node#2611']
        self.assertEqual(merged['period_commits'], [])
        self.assertEqual(merged['merged_at'], merged_at)
        self.assertEqual(merged['description'], 'Fixture description 2611')
        for pr in records:
            if pr.number != 2611:
                self.assertEqual(len(by_id[pr.id]['period_commits']), 1)
                self.assertEqual(by_id[pr.id]['period_commits'][0]['message_status'], MessageStatus.OK)
        self.assertIn(merged_at, output.read_text())

    def test_redaction_covers_latest_view_and_all_new_snapshot_files(self):
        secret = 'ghp_' + 'a' * 36
        details, _ = self.details(body=secret, title='fixture ' + secret)
        details.period_commits[0].message = 'fixture credential ' + secret
        snapshot = Snapshot(self.home / 'input.json',
                            '2026-10-16T00:00:00+08:00', '2026-10-17T00:00:00+08:00')
        report = DetailsReport(snapshot, [self.pr.id], [details])
        output = self.home / 'selected.md'
        self.assertIsNone(pr_report.write_report(output, report, False))
        # A latest view left by the old writer must not leak into a new previous.md.
        output.write_text('legacy title ' + secret)
        self.assertIsNone(pr_report.write_report(output, report, True))
        files = [output, *self.home.glob('.history/*/*/*')]
        self.assertTrue(any(path.name == 'previous.md' for path in files))
        self.assertTrue(any(path.name == 'evidence.json' for path in files))
        for path in files:
            with self.subTest(file=path):
                text = path.read_text()
                self.assertNotIn(secret, text)
                self.assertIn('[REDACTED:github-token]', text)
        # Writing a sanitized artifact must not mutate its source evidence.
        self.assertIn(secret, details.period_commits[0].message)

    def test_cli_rejects_unknown_pr_before_network_and_redacts_archive(self):
        source = self.home / 'evidence.json'
        source.write_text(json.dumps({'start': '2026-10-16T00:00:00+08:00',
            'end_exclusive': '2026-10-17T00:00:00+08:00',
            'pull_requests': [json_evidence(self.pr, False)]}))
        output = self.home / 'selected.md'
        argv = ['pr_details.py', '--evidence', str(source), '--pr', 'acme/node#2', '--out', str(output)]
        with patch.object(sys, 'argv', argv), patch.object(prs, 'api') as api, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            pr_details.main()
        self.assertEqual(error.exception.code, 2)
        api.assert_not_called()
        argv[4] = 'acme/node#1'
        secret = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'
        with patch.object(sys, 'argv', argv), \
             patch.object(prs, 'api', return_value={'number': 1, 'title': 'Fix RPC 1',
                                                    'body': secret, 'state': 'open', 'head': {}}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pr_details.main(), 0)
        snapshot = next(self.home.glob('.history/*/*/evidence.json'))
        self.assertNotIn(secret, snapshot.read_text())
        self.assertIn('period_commits', snapshot.read_text())


GH_STUB = """#!/usr/bin/env python3
# Fake gh: replays canned responses so tests never reach GitHub
import json, sys
args = " ".join(sys.argv)
if "search/issues" in args:
    print(json.dumps({"total_count": 2, "items": [
        {"number": 43, "title": "RPC 空结果编码", "state": "open",
         "html_url": "https://github.com/acme/node/pull/43",
         "repository_url": "https://api.github.com/repos/acme/node",
         "updated_at": "2026-10-16T12:00:00Z", "user": {"login": "someone-else"}},
        {"number": 7, "title": "只评审过的 PR", "state": "closed",
         "html_url": "https://github.com/acme/sdk/pull/7",
         "repository_url": "https://api.github.com/repos/acme/sdk",
         "updated_at": "2026-10-16T13:00:00Z", "user": {"login": "me"}}]}))
elif "pulls/43/commits" in args:
    print(json.dumps([[
        {"sha": "MINE", "author": {"login": "me"},
         "commit": {"message": "fix(rpc): 空结果返回空数组",
                    "author": {"name": "Test User", "email": "dev@example.com",
                               "date": "2026-10-16T01:00:00Z"},
                    "committer": {"date": "2026-10-16T02:00:00Z"}}},
        {"sha": "b" * 40, "author": {"login": "other"},
         "commit": {"message": "chore: 同事的提交",
                    "author": {"name": "Colleague", "email": "c@example.com",
                               "date": "2026-10-16T01:00:00Z"},
                    "committer": {"date": "2026-10-16T03:00:00Z"}}}]]))
elif "pulls/7/commits" in args:
    print(json.dumps([[]]))
elif args.rstrip().endswith("user"):
    print(json.dumps({"login": "me"}))
else:
    sys.exit("unexpected gh call: " + args)
"""


class TestEndToEndDiscovery(unittest.TestCase):
    """Real CLI plus traversal: gh is a stub executable, git a local bare remote, no network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = make_repo(self.base / "repos", "node")
        commit(self.repo, "fix(rpc): 空结果返回空数组", committed="2026-10-16T10:00:00+08:00")
        self.sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        commit(self.repo, "wip: 还没开 PR 的工作", committed="2026-10-16T18:00:00+08:00",
               filename="wip.txt")
        mirror = self.base / "mirror.git"
        subprocess.run(["git", "init", "--bare", "-q", str(mirror)], check=True)
        git(self.repo, "remote", "add", "origin", str(mirror))
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        # Maps to owner/repo but is excluded from fetch so the test stays offline
        git(self.repo, "remote", "add", "github", "https://github.com/acme/node.git")
        git(self.repo, "config", "remote.github.skipFetchAll", "true")

        stub = self.base / "bin" / "gh"
        stub.parent.mkdir()
        stub.write_text(GH_STUB.replace("MINE", self.sha))
        stub.chmod(0o755)
        self.cfg = self.base / "config.json"
        self.cfg.write_text(json.dumps({
            "author_emails": ["dev@example.com"], "repos_root": str(self.base / "repos"),
            "reports_dir": str(self.base / "reports"),
            "sources": {"git": True, "claude": False, "codex": False, "dsh": False}}))
        self.env = {"HOME": str(self.base), "DAILY_REPORT_CONFIG": str(self.cfg),
                    "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_level_traversal_through_the_real_cli(self):
        result = run(["--date", "2026-10-16", "--prs"], env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = (self.base / "reports/.evidence/20261016.md").read_text()
        self.assertIn("acme/node#43", evidence)
        self.assertIn("本期无本人提交", evidence, "review only PRs stay for screening")
        self.assertIn("fix(rpc): 空结果返回空数组", evidence)
        self.assertIn("来自 acme/node#43", evidence)
        self.assertNotIn("同事的提交", evidence)
        self.assertNotIn("wip: 还没开 PR 的工作", evidence, "commits outside a PR stay out")
        self.assertIn("未在本次 PR 检索结果中找到关联 1", evidence)

        snapshot = next((self.base / "reports/.evidence").glob(".history/*/*/evidence.json"))
        data = json.loads(snapshot.read_text())
        self.assertEqual([c["full_sha"] for c in data["commits"]], [self.sha])
        self.assertEqual(data["commits"][0]["pr_ids"], ["acme/node#43"])
        self.assertEqual(data["commits"][0]["files"], ["f.txt"], "changes resolved by SHA")
        self.assertEqual(data["diagnostics"]["git"]["local_unassociated"], 1)
        self.assertEqual(data["pr_diagnostics"]["prs_with_commits"], 1)

    def test_explicit_cli_author_overrides_login(self):
        result = run(['--date', '2026-10-16', '--prs', '--author', 'someone-else@example.com',
                      '--print'], env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fix(rpc): 空结果返回空数组', result.stdout)
        self.assertIn('本期无本人提交 2', result.stdout)


if __name__ == '__main__':
    unittest.main()
