import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ai_brief.source_followup import PreflightError, source_followup
from ai_brief.source_identity import source_identity


class SourceFollowupTests(unittest.TestCase):
    since = "2026-11-18T00:00:00Z"
    start = "https://api.github.com/repos/org/repo/pulls?sort=updated&direction=desc"
    next_page = "https://api.github.com/repositories/1/pulls?page=2&sort=updated&direction=desc"
    article = {"html_url": "https://github.com/org/repo/pull/1", "updated_at": "2026-11-18T01:00:00Z"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ops = self.root / "operations"
        self.ops.mkdir()
        self.ledger = {"request_id": "original", "scheduled_for": "2026-11-19T00:00:00Z", "operations": []}

    def receipt(self, url, body, link=None, status=200):
        key = source_identity(url)["hash"]
        oid = f"source-{len(self.ledger['operations'])}"
        folder = self.ops / oid
        folder.mkdir()
        headers = f"HTTP/2 {status}\r\n"
        if link:
            headers += f'Link: <{link}>; rel="next"\r\n'
        result = dict(operation_id=oid, request_id="original", url=url, input_sha256=key, status="ok",
                      http_status=status, response_complete=True, recording_errors=[], artifacts={})
        for name, data in [("response.headers.bin", headers.encode()), ("response.body.bin", json.dumps(body).encode())]:
            (folder / name).write_bytes(data)
            result["artifacts"][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        raw = json.dumps(result).encode()
        (folder / "result.json").write_bytes(raw)
        self.ledger["operations"].append(dict(operation_id=oid, operation_key=key, input_hash=key,
                                              kind="source", phase="acknowledged", result_file=hashlib.sha256(raw).hexdigest()))
        return folder

    def follow(self):
        path = self.root / "ledger.json"
        path.write_text(json.dumps(self.ledger), encoding="utf-8")
        return source_followup(path, self.ops, self.since)

    def test_next_requires_original_read_and_reuses_event_evidence(self):
        self.receipt(self.start, [self.article], self.next_page)
        result = self.follow()
        self.assertEqual(result["next_requests"], [dict(input=source_identity(self.next_page)["value"], reason="pagination")])
        self.assertFalse(result["pagination"][0]["next_read"])
        self.receipt(self.next_page, [self.article])
        self.receipt("https://api.github.com/repos/org/repo/pulls/1", {**self.article, "title": "updated"})
        result = self.follow()
        self.assertTrue(result["pagination"][0]["next_read"])
        self.assertEqual(result["next_requests"], [])
        self.assertEqual(result["repeated_events"][self.article["html_url"]], ["source-0", "source-1", "source-2"])
        self.assertEqual(list(result["identical_bodies"].values()), [["source-0", "source-1"]])

    def test_cutoff_needs_proven_descending_order_and_frozen_window(self):
        self.receipt(self.start, [self.article, {**self.article, "updated_at": "2026-11-17T12:00:00Z"}], self.next_page)
        self.assertTrue(self.follow()["pagination"][0]["window_cutoff"])
        self.assertEqual(self.follow()["next_requests"], [])
        self.since = "2026-11-20T00:00:00Z"
        with self.assertRaisesRegex(PreflightError, "scheduled run"):
            self.follow()

    def test_failed_and_missing_pages_remain_gaps_and_official_alternates_need_reads(self):
        self.receipt(self.start, [self.article], self.next_page)
        self.receipt(self.next_page, [], status=403)
        self.receipt("https://z.ai/blog", {}, status=404)
        result = self.follow()
        self.assertIn("next_response_unverified", {g["reason"] for g in result["gaps"]})
        self.assertEqual(len(result["next_requests"]), 2)
        self.receipt(result["next_requests"][0]["input"]["url"], {})
        self.assertEqual(len(self.follow()["next_requests"]), 1)

    def test_original_bytes_and_operation_ids_are_verified(self):
        folder = self.receipt(self.start, [self.article], self.start)
        self.assertIn("pagination_cycle", {g["reason"] for g in self.follow()["gaps"]})
        (folder / "response.body.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(PreflightError, "hash differs"):
            self.follow()
        self.ledger["operations"].append(self.ledger["operations"][0])
        with self.assertRaisesRegex(PreflightError, "identity differs"):
            self.follow()
