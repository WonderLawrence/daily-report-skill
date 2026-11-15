"""Receipts and bounded evidence reads, including the real CLI; no network."""
import copy
import json
import subprocess
import sys
from pathlib import Path

from helpers import ROOT, SessionFixture, run
from daily_report import evidence
from daily_report.evidence_reader import encode, load_input, read_page


class TestEvidenceReader(SessionFixture):
    def setUp(self):
        super().setUp()
        self.session_id = "019d092a-b74b-7172-9bf3-05172815cd55"
        self.long_text = '中文说明\n"quoted"\\内容🙂' * 900
        self.collection = {
            "schema_version": 1, "generated_at": "2026-10-16T12:00:00+08:00",
            "start": "2026-10-16T00:00:00+08:00", "end_exclusive": "2026-10-17T00:00:00+08:00",
            "pull_requests": [{"id": "acme/node#1", "title": "Fix RPC",
                               "url": "https://github.com/acme/node/pull/1", "associated_commits": []}],
            "commits": [],
            "sessions": [{"id": self.session_id, "agent": "codex", "project": "/demo",
                          "events": [{"kind": "user_request", "text": self.long_text, "line": i + 1,
                                      "source": "/fixture/session.jsonl", "at": "2026-10-16T12:00:00+08:00"}
                                     for i in range(23)]}],
            "diagnostics": {"git": {"commits_without_local_copy": 8}}}
        self.source = self.home / "evidence.json"
        self.source.write_text(json.dumps(self.collection, ensure_ascii=False))

    def command(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/read_evidence.py"),
                               "--evidence", str(self.source), *args], text=True, capture_output=True)

    def test_default_inspection_exposes_actual_keys_without_dumping_content(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["metadata"]["generated_at"], self.collection["generated_at"])
        self.assertEqual(data["fields"]["sessions"]["length"], 1)
        self.assertNotIn(self.long_text[:50], result.stdout)
        self.assertEqual(read_page(self.source, field="generated_at")["data"],
                         self.collection["generated_at"])

    def test_wrong_field_and_wrong_id_fail_loudly_without_partial_stdout(self):
        for args in (("--field", "generated"), ("--field", "details"),
                     ("--pr", "acme/node#404"), ("--session", "missing"),
                     ("--field", "sessions.0.events.999.text"),
                     ("--field", "commits", "--fields", "misspelled")):
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
        self.assertIn("generated_at", self.command("--field", "generated").stderr)
        self.assertEqual(read_page(self.source, field="commits", projection=["full_sha"])["data"], [])

    def test_short_ids_require_unique_match(self):
        page = read_page(self.source, session="2815cd55", field="agent")
        self.assertEqual(page["data"], "codex")
        duplicate = copy.deepcopy(self.collection["sessions"][0])
        duplicate["id"] = "019d092a-c001-7000-8000-00002815cd55"
        self.collection["sessions"].append(duplicate)
        self.source.write_text(json.dumps(self.collection))
        with self.assertRaisesRegex(ValueError, "匹配多个"):
            read_page(self.source, session="2815cd55")
        self.assertEqual(read_page(self.source, session=self.session_id)["metadata"]["id"], self.session_id)

    def test_projected_event_pages_preserve_order_and_original_locators(self):
        items, offset = [], 0
        while True:
            page = read_page(self.source, session=self.session_id, field="events",
                             projection=["kind", "at", "source", "line"], offset=offset,
                             limit=8, max_chars=1600)
            self.assertLessEqual(len(encode(page)), 1600)
            self.assertEqual(page["total"], 23)
            items.extend(page["data"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual([item["line"] for item in items], list(range(1, 24)))
        self.assertTrue(all(item["source"] == "/fixture/session.jsonl" for item in items))

    def test_long_text_can_be_reassembled_exactly_within_encoded_budget(self):
        original = self.source.read_bytes()
        chunks, offset = [], 0
        while True:
            page = read_page(self.source, session=self.session_id, field="events.0.text",
                             offset=offset, limit=5000, max_chars=1200)
            self.assertLessEqual(len(encode(page)), 1200)
            chunks.append(page["data"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(chunks), self.long_text)
        self.assertEqual(self.source.read_bytes(), original)

    def test_oversized_single_item_requires_narrower_read_instead_of_truncation(self):
        result = self.command("--session", self.session_id, "--field", "events", "--max-chars", "1200")
        self.assertEqual(result.returncode, 2)
        self.assertIn("events.0.text", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_pr_detail_description_and_partial_status_are_readable(self):
        data = {"schema_version": 1, "generated_at": "now", "selected_prs": ["acme/node#1"],
                "status": "partial", "summary": {"failed_commit_messages": 1},
                "pull_requests": [{"id": "acme/node#1", "title": "Fix RPC", "description": self.long_text,
                                   "url": "https://github.com/acme/node/pull/1", "period_commits": []}],
                "failures": []}
        self.source.write_text(json.dumps(data))
        self.assertEqual(read_page(self.source)["metadata"]["status"], "partial")
        page = read_page(self.source, pr="https://github.com/acme/node/pull/1",
                         field="description", limit=25)
        self.assertEqual(page["data"], self.long_text[:25])
        self.assertEqual(page["next_offset"], 25)
        self.assertEqual(read_page(self.source, pr="acme/node#1", field="period_commits",
                                   projection=["message_status"])["data"], [])

    def test_receipt_tracks_its_own_snapshot_after_force_update(self):
        markdown = self.home / "space in path" / "latest.md"
        first = evidence.write_evidence(markdown, "fixture", False, self.collection)
        receipt = self.home / "run.json"
        receipt.write_text(json.dumps(first.receipt()))
        self.collection["generated_at"] = "later"
        second = evidence.write_evidence(markdown, "new", True, self.collection)
        self.assertNotEqual(first.evidence_path, second.evidence_path)
        self.assertEqual(read_page(receipt, field="generated_at")["data"], "2026-10-16T12:00:00+08:00")
        self.assertEqual(read_page(second.evidence_path, field="generated_at")["data"], "later")
        self.assertEqual(load_input(receipt)[0], first.evidence_path)

    def test_bad_inputs_and_pagination_options_have_explicit_errors(self):
        for change in (dict(offset=-1), dict(limit=0), dict(max_chars=10), dict(offset=999)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_page(self.source, field="sessions", **change)
        for value in ([], {"schema_version": 2, "sessions": []},
                      {"kind": "evidence_receipt", "schema_version": 1},
                      {"kind": "evidence_receipt", "schema_version": 1, "evidence_path": str(self.source)}):
            self.source.write_text(json.dumps(value))
            result = self.command()
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
        self.source.write_text("{ broken")
        self.assertEqual(self.command().returncode, 2)
        with self.assertRaisesRegex(ValueError, "JSON 文件"):
            load_input(self.home)


class TestCollectionReceipts(SessionFixture):
    def test_real_cli_receipt_roundtrip_and_errors_do_not_emit_success(self):
        cfg = self.home / "config.json"
        cfg.write_text(json.dumps({"reports_dir": str(self.home / "reports")}))
        args = ["--date", "2026-10-16", "--config", str(cfg), "--no-git", "--no-sessions", "--json"]
        result = run(args, env={"HOME": str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["counts"], {"commits": 0, "sessions": 0})
        self.assertIn("[ok]", result.stderr)
        receipt_file = self.home / "run.json"
        receipt_file.write_text(result.stdout)
        self.assertEqual(read_page(receipt_file)["artifact_kind"], "collection")
        self.assertTrue(Path(receipt["snapshot_markdown_path"]).is_file())
        failed = run(args, env={"HOME": str(self.home)})
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(failed.stdout, "")
        self.assertIn("--overwrite", failed.stderr)
        for flag in ("--print", "--whoami"):
            conflict = run([*args, flag], env={"HOME": str(self.home)})
            self.assertEqual(conflict.returncode, 2)
            self.assertEqual(conflict.stdout, "")


if __name__ == "__main__":
    import unittest
    unittest.main()
