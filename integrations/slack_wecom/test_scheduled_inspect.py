"""认证日期快照的离线回归；不使用真实网络、凭据、发送或生产账本。"""
import hashlib
import io
import json
import os
import time
from pathlib import Path
import sqlite3
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import scheduled_client
import scheduled_socket
import test_scheduled_delivery as delivery_fixtures


class InspectionTests(unittest.TestCase):
    setUp = delivery_fixtures.ScheduledTests.setUp
    tearDown = delivery_fixtures.ScheduledTests.tearDown
    queue = delivery_fixtures.ScheduledTests.queue
    send = delivery_fixtures.ScheduledTests.send
    admit = delivery_fixtures.ScheduledTests.admit
    advance = delivery_fixtures.ScheduledTests.advance
    legacy_claim = delivery_fixtures.ScheduledTests.legacy_claim

    def inspect(self, request_id="isolated-bundle-fixture", day="2026-11-01"):
        return self.q.inspect(request_id, day)

    def assert_read_only(self, operation):
        before, changes = list(self.store.db.iterdump()), self.store.db.total_changes
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE}
        self.store.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        try:
            with patch.object(self.q, "verify", side_effect=AssertionError("must not verify externally")), \
                    patch.object(self.q, "tick", side_effect=AssertionError("must not tick")), \
                    patch.object(self.q, "admit", side_effect=AssertionError("must not admit")):
                result = operation()
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertFalse(self.store.db.in_transaction)
        return result

    def test_empty_snapshot_is_exact_and_has_zero_business_writes(self):
        result = self.assert_read_only(self.inspect)
        self.assertEqual(result, {"read_context": {"scheduler_scope_sha256": self.store.get("scheduled_delivery_scope_v1")},
            "observed_at": int(self.now[0] * 1000), "complete": True, "scheduler_day_claim": None, "scheduler": None})
        self.assertEqual(self.calls, [])

    def test_original_snapshot_is_bound_complete_and_does_not_expose_content(self):
        self.admit()
        result = self.assert_read_only(self.inspect)
        self.assertEqual(result["scheduler"], self.q.status("isolated-bundle-fixture"))
        self.assertEqual(result["scheduler_day_claim"], {"request_id": "isolated-bundle-fixture", "report_date": "2026-11-01",
            "bundle_sha256": hashlib.sha256(self.bundle).hexdigest(), "report_commit": "a" * 40})
        encoded = json.dumps(result)
        for private in (self.plan["text"], self.plan["notice_key"], "bundle_base64", "poster_base64", "webhook", "actor_uid"):
            self.assertNotIn(private, encoded)
        self.assertEqual(self.calls, [])

    def test_partial_and_confirmed_completion_remain_distinct(self):
        self.admit(); self.q.tick()
        partial = self.assert_read_only(self.inspect)["scheduler"]
        self.assertEqual((partial["parts_sent"], partial["parts_total"], partial["phase"]), (1, 2, "pending"))
        self.advance()
        complete = self.assert_read_only(self.inspect)["scheduler"]
        self.assertEqual((complete["status"], complete["phase"], complete["parts_sent"]), ("sent", "complete", 2))
        self.assertEqual([kind for kind, _ in self.calls], ["text", "image"])

    def manual_successor(self, revision=1, request_id="manual-successor", n=2):
        import relay
        import runtime
        from resume import assert_reconciled
        from test_notice_connector import NOTICE_CONFIG, notice
        from test_relay import FakeSlack
        slack = FakeSlack()
        slack.messages = [notice(n, revision=revision)]
        slack.download_image = lambda plan: self.poster
        slack.verify = lambda: None
        receiver = relay.Relay(self.store, slack, self.wecom, NOTICE_CONFIG,
            clock=lambda: self.now[0], sleeper=lambda seconds: self.now.__setitem__(0, self.now[0] + seconds))
        controller = runtime.ControlledRelay(relay, receiver, "a" * 40,
            scheduled=SimpleNamespace(assert_reconciled=lambda: assert_reconciled(self.store)),
            automatic_delivery="scheduled_only")
        controller.dispatch("/control/activate", {"release": "a" * 40, "deadline": time.monotonic() + 60, "nonce": None}, os.getuid())
        result = controller.dispatch("/manual/slack-message", {"request_id": request_id,
            "message_ts": slack.messages[0]["ts"], "allow_repeat": True, "reason": "explicit test repeat"}, os.getuid())
        self.assertEqual(result["status"], "sent")
        return result

    def test_completed_manual_repeat_preserves_future_absence_without_reclaiming_history(self):
        self.admit(); self.q.tick(); self.advance()
        self.assertIsNone(self.inspect("next-day-fixture", "2026-11-02")["scheduler"])
        original = self.q.status("isolated-bundle-fixture")
        self.manual_successor()
        result = self.assert_read_only(lambda: self.inspect("next-day-fixture", "2026-11-02"))
        self.assertTrue(result["complete"])
        self.assertIsNone(result["scheduler"])
        self.assertIsNone(result["scheduler_day_claim"])
        self.assertEqual(self.q.status("isolated-bundle-fixture"), original)
        with self.assertRaises(ValueError): self.inspect()
        self.assertEqual([kind for kind, _ in self.calls], ["text", "image", "text", "image"])

    def test_completed_manual_revisions_keep_attributed_historical_edition_claims(self):
        self.admit(); self.q.tick(); self.advance()
        self.manual_successor(revision=1, request_id="repeat-first", n=2)
        self.manual_successor(revision=2, request_id="repeat-second", n=3)
        self.manual_successor(revision=1, request_id="repeat-third", n=4)
        self.assertIsNone(self.assert_read_only(lambda: self.inspect("next-day-fixture", "2026-11-02"))["scheduler"])
        with self.store.db:
            self.store.db.execute("UPDATE requests SET status='unknown' WHERE id='repeat-second'")
        def rejected():
            with self.assertRaises(ValueError): self.inspect("next-day-fixture", "2026-11-02")
        self.assert_read_only(rejected)
        self.assertEqual(len(self.calls), 8)

    def test_manual_claim_successor_must_have_complete_original_and_bound_receipts(self):
        self.admit(); self.q.tick(); self.advance(); self.manual_successor()
        mutations = [
            "UPDATE scheduled_notices SET phase='unknown'",
            "UPDATE requests SET status='unknown' WHERE mode='scheduled'",
            "UPDATE parts SET status='unknown' WHERE request_id='isolated-bundle-fixture'",
            "DELETE FROM parts WHERE request_id='isolated-bundle-fixture'",
            "UPDATE requests SET status='unknown' WHERE id='manual-successor'",
            "UPDATE requests SET status='partial' WHERE id='manual-successor'",
            "UPDATE requests SET status='failed' WHERE id='manual-successor'",
            "UPDATE requests SET mode='auto' WHERE id='manual-successor'",
            "UPDATE requests SET reason_hash=NULL WHERE id='manual-successor'",
            "UPDATE requests SET source_key='wrong' WHERE id='manual-successor'",
            "UPDATE requests SET created=1 WHERE id='manual-successor'",
            "UPDATE requests SET parts_sent=1 WHERE id='manual-successor'",
            "DELETE FROM requests WHERE id='manual-successor'",
            "DELETE FROM parts WHERE request_id='manual-successor'",
            "UPDATE image_parts SET status='unknown' WHERE request_id='manual-successor'",
            "UPDATE image_parts SET byte_size=0 WHERE request_id='manual-successor'",
            "UPDATE meta SET value='foreign-owner' WHERE key LIKE 'notice_day:%'",
        ]
        for sql in mutations:
            with self.subTest(sql=sql):
                backup = sqlite3.connect(":memory:"); self.store.db.backup(backup)
                try:
                    with self.store.db: self.store.db.execute(sql)
                    def rejected():
                        with self.assertRaises((ValueError, TypeError)): self.inspect("next-day-fixture", "2026-11-02")
                    self.assert_read_only(rejected)
                finally:
                    backup.backup(self.store.db); backup.close()

    def test_unknown_is_readable_and_never_replayed(self):
        self.admit(); self.outcomes = [TimeoutError()]; self.q.tick()
        for _ in range(3):
            result = self.assert_read_only(self.inspect)
            self.assertEqual(result["scheduler"]["status"], "unknown")
            self.assertEqual(result["scheduler"]["phase"], "unknown")
        self.assertIsNone(self.advance(99999))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.inspect("next-day-fixture", "2026-11-02")["scheduler"], None)

    def test_different_request_same_day_or_original_request_other_day_is_not_empty(self):
        self.admit()
        for rid, day in (("different-fixture", "2026-11-01"), ("isolated-bundle-fixture", "2026-11-02")):
            before = list(self.store.db.iterdump())
            with self.assertRaises(ValueError):
                self.inspect(rid, day)
            self.assertEqual(list(self.store.db.iterdump()), before)

    def test_unmapped_native_unknown_blocks_absence_but_not_bound_other_report(self):
        self.admit()
        with self.store.db:
            self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('legacy','hash','auto',1,'unknown')")
        self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["status"], "queued")
        with self.assertRaises(ValueError):
            self.inspect("next-day-fixture", "2026-11-02")

    def test_legacy_claim_requires_canonical_mapping_and_original_request(self):
        self.legacy_claim()
        with self.assertRaises(ValueError):
            self.inspect()
        edition = self.store.db.execute("SELECT key FROM meta WHERE key LIKE 'notice_edition:%'").fetchone()[0]
        suffix = edition.split(":", 1)[1]
        for day in ("2026-02-30", "20261101", "2026-11-01"):
            self.store.set("notice_edition_date:" + suffix, day)
            with self.assertRaises(ValueError):
                self.inspect()

    def test_orphan_claims_and_parts_never_prove_empty(self):
        cases = [
            ("INSERT INTO meta VALUES (?,?)", ("notice_day:" + "a" * 64, "missing")),
            ("INSERT INTO meta VALUES (?,?)", ("notice_edition_date:" + "a" * 64, "2026-11-01")),
            ("INSERT INTO parts VALUES (?,?,?,?)", ("missing", 0, "hash", "unknown")),
            ("INSERT INTO image_parts(request_id,file_id,ordinal,status) VALUES (?,?,?,?)", ("missing", "poster", 0, "sent")),
            ("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES (?,?,?,?,?)", ("missing", "hash", "scheduled", 1, "queued")),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql):
                backup = sqlite3.connect(":memory:"); self.store.db.backup(backup)
                try:
                    with self.store.db:
                        self.store.db.execute(sql, args)
                    before = list(self.store.db.iterdump())
                    with self.assertRaises(ValueError): self.inspect()
                    self.assertEqual(list(self.store.db.iterdump()), before)
                finally:
                    backup.backup(self.store.db); backup.close()

    def test_corrupted_original_binding_or_component_receipt_is_rejected(self):
        self.admit()
        changes = [
            "UPDATE requests SET intent_hash='wrong'",
            "UPDATE requests SET mode='auto'",
            "UPDATE requests SET parts_sent=1",
            "UPDATE requests SET parts_total=3",
            "UPDATE requests SET status='sent'",
            "DELETE FROM requests",
            "DELETE FROM parts",
            "UPDATE parts SET content_hash='wrong'",
            "UPDATE parts SET status='unknown'",
            "UPDATE image_parts SET ordinal=0",
            "UPDATE image_parts SET byte_size=1",
            "UPDATE image_parts SET error_code='private unexpected message'",
            "UPDATE scheduled_notices SET bundle=x'00'",
            "UPDATE scheduled_notices SET poster=x'00'",
            "UPDATE scheduled_notices SET due=1",
            "UPDATE scheduled_notices SET report_date='2026-11-02'",
            "UPDATE scheduled_notices SET phase='complete'",
            "UPDATE scheduled_notices SET last_error='https://private.invalid/secret'",
            "DELETE FROM meta WHERE key LIKE 'notice_day:%'",
            "UPDATE meta SET value='other-fixture' WHERE key LIKE 'notice_edition:%'",
        ]
        for sql in changes:
            with self.subTest(sql=sql):
                backup = sqlite3.connect(":memory:"); self.store.db.backup(backup)
                try:
                    with self.store.db: self.store.db.execute(sql)
                    before = list(self.store.db.iterdump())
                    with self.assertRaises((ValueError, TypeError, KeyError)): self.inspect()
                    self.assertEqual(list(self.store.db.iterdump()), before)
                finally:
                    backup.backup(self.store.db); backup.close()

    def test_missing_or_changed_scope_is_not_absence(self):
        self.store.set("scheduled_delivery_scope_v1", "f" * 64)
        with self.assertRaises(ValueError): self.inspect()
        with self.store.db: self.store.db.execute("DELETE FROM meta WHERE key='scheduled_delivery_scope_v1'")
        with self.assertRaises(ValueError): self.inspect()

    def test_invalid_or_overlarge_clock_does_not_create_snapshot(self):
        for now in (float("nan"), float("inf"), 0, -1, 9007199254740992):
            self.now[0] = now
            with self.assertRaises(ValueError): self.inspect()
            self.assertFalse(self.store.db.in_transaction)

    def test_inspection_cannot_commit_or_rollback_caller_transaction(self):
        self.store.db.execute("BEGIN")
        self.store.db.execute("INSERT INTO meta VALUES ('caller_transaction','preserve')")
        try:
            with self.assertRaises(ValueError): self.inspect()
            self.assertTrue(self.store.db.in_transaction)
            self.assertEqual(self.store.get("caller_transaction"), "preserve")
        finally: self.store.db.rollback()

    def test_concurrent_commit_and_delayed_reply_preserve_one_read_snapshot(self):
        self.admit()
        self.store.db.execute("PRAGMA journal_mode=WAL")
        writer = sqlite3.connect(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(writer.close)
        initial_time, changed = self.now[0], []
        def interleave(sql):
            if sql.startswith("SELECT request_id,report_date FROM scheduled_notices") and not changed:
                changed.append(True)
                with writer:
                    writer.execute("UPDATE parts SET status='sent'")
                    writer.execute("UPDATE image_parts SET status='sent'")
                    writer.execute("UPDATE requests SET status='sent',parts_sent=2")
                    writer.execute("UPDATE scheduled_notices SET phase='complete'")
                self.now[0] += 120
        self.store.db.set_trace_callback(interleave)
        try: first = self.inspect()
        finally: self.store.db.set_trace_callback(None)
        self.assertEqual(first["scheduler"]["parts_sent"], 0)
        self.assertEqual(first["scheduler"]["status"], "queued")
        self.assertEqual(first["observed_at"], int(initial_time * 1000))
        second = self.inspect()
        self.assertEqual((second["scheduler"]["parts_sent"], second["scheduler"]["status"]), (2, "sent"))
        self.assertGreater(second["observed_at"], first["observed_at"])
        self.assertEqual(self.calls, [])


class InspectionProtocolTests(unittest.TestCase):
    request = {"action": "inspect", "request_id": "original-fixture", "report_date": "2026-11-01"}

    def snapshot(self):
        return {"read_context": {"scheduler_scope_sha256": "a" * 64}, "observed_at": 1793491200000,
                "complete": True, "scheduler_day_claim": None, "scheduler": None}

    def test_exact_inspect_request_works_while_paused_without_admission(self):
        queue = Mock(); queue.inspect.return_value = self.snapshot()
        result = scheduled_socket.dispatch(json.dumps(self.request).encode(), queue,
            lambda: self.fail("read must not consult admission gate"))
        self.assertEqual(result, self.snapshot())
        queue.inspect.assert_called_once_with("original-fixture", "2026-11-01")
        queue.admit.assert_not_called(); queue.tick.assert_not_called(); queue.status.assert_not_called()

    def test_exact_schema_identity_and_calendar_are_required_before_queue_access(self):
        queue = Mock()
        requests = [{**self.request, "hash": "a" * 64}, {k: v for k, v in self.request.items() if k != "report_date"}]
        requests += [{**self.request, "request_id": value} for value in ("short", "Aoriginal", "a" * 81, None, True)]
        requests += [{**self.request, "report_date": value} for value in ("2026-02-30", "2026-2-01", "20261101", "2026-W44-7", None, 20261101)]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                scheduled_socket.dispatch(json.dumps(request).encode(), queue)
        with self.assertRaises(ValueError):
            scheduled_socket.dispatch(b'{"action":"inspect","request_id":"original-fixture","report_date":"2026-11-01","report_date":"2026-11-02"}', queue)
        self.assertEqual(queue.mock_calls, [])

    def test_wrong_peer_is_rejected_before_read_and_errors_leak_no_details(self):
        queue, connection = Mock(), Mock()
        with patch.object(scheduled_socket, "peer_uid", return_value=123), \
                patch.object(scheduled_socket, "bounded_read", side_effect=AssertionError("must not read")):
            scheduled_socket.handle(connection, 456, queue)
        self.assertEqual(queue.mock_calls, []); connection.sendall.assert_not_called()
        queue.inspect.side_effect = ValueError("private request or original content")
        with patch.object(scheduled_socket, "peer_uid", return_value=456), \
                patch.object(scheduled_socket, "bounded_read", return_value=json.dumps(self.request).encode()):
            scheduled_socket.handle(connection, 456, queue, lambda: False)
        self.assertEqual(json.loads(connection.sendall.call_args.args[0]),
            {"status": "not_verified", "error": "scheduled_request_rejected"})

    def test_client_accepts_snapshot_without_treating_legacy_not_found_as_empty(self):
        request = json.dumps(self.request).encode()
        self.assertEqual(scheduled_client.read_result(json.dumps(self.snapshot()).encode(), request), self.snapshot())
        for change in ({"complete": False}, {"observed_at": True}, {"observed_at": float("nan")},
                       {"scheduler_day_claim": {}}, {"read_context": {"scheduler_scope_sha256": "wrong"}}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                scheduled_client.read_result(json.dumps({**self.snapshot(), **change}).encode(), request)
        with self.assertRaises(ValueError):
            scheduled_client.read_result(b'{"status":"not_found"}', request)
        self.assertEqual(scheduled_client.read_result(b'{"status":"not_found"}', b'{"action":"status","request_id":"original-fixture"}'), {"status": "not_found"})

    def test_client_preserves_bound_unknown_and_rejects_mixed_or_incomplete_binding(self):
        request = json.dumps(self.request).encode()
        claim = {"request_id": "original-fixture", "report_date": "2026-11-01", "bundle_sha256": "b" * 64, "report_commit": "c" * 40}
        scheduler = {**claim, "status": "unknown", "phase": "unknown", "parts_total": 2, "parts_sent": 1,
            "next_attempt_at": 1793491200.0, "attempts": 0, "last_error": None,
            "images": [{"file_id": "scheduled-poster", "status": "unknown", "error": None}]}
        result = {**self.snapshot(), "scheduler_day_claim": claim, "scheduler": scheduler}
        self.assertEqual(scheduled_client.read_result(json.dumps(result).encode(), request), result)
        for change in ({"request_id": "another-fixture"}, {"report_date": "2026-11-02"},
                       {"status": "sent", "phase": "complete", "parts_sent": 2}, {"parts_sent": 3}, {"text": "private"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                scheduled_client.read_result(json.dumps({**result, "scheduler": {**scheduler, **change}}).encode(), request)

    def test_client_main_succeeds_for_statusless_snapshot(self):
        output = io.StringIO()
        with patch.object(scheduled_client, "query", return_value=self.snapshot()), \
                patch.object(scheduled_client.sys, "stdin", Mock(buffer=io.BytesIO(json.dumps(self.request).encode()))), \
                patch.object(scheduled_client.sys, "stdout", output):
            self.assertEqual(scheduled_client.main(["--socket", "/fixed/scheduled.sock", "--reader-uid", "456"]), 0)
        self.assertEqual(json.loads(output.getvalue()), self.snapshot())

    def test_client_rejects_wrong_kernel_reader_before_sending_request(self):
        connection = Mock()
        connection.getsockopt.return_value = struct.pack("3i", 1, 999, 999)
        manager = Mock(__enter__=Mock(return_value=connection), __exit__=Mock(return_value=False))
        def info(path):
            return SimpleNamespace(st_uid=456, st_mode=(stat.S_IFSOCK | 0o666) if path.name == "scheduled.sock" else (stat.S_IFDIR | 0o711))
        with patch.object(Path, "lstat", info), patch.object(scheduled_client.socket, "socket", return_value=manager), \
                self.assertRaisesRegex(ValueError, "unexpected_reader_identity"):
            scheduled_client.query(Path("/fixed/scheduled.sock"), 456, json.dumps(self.request).encode())
        connection.sendall.assert_not_called()


if __name__ == "__main__":
    unittest.main()
