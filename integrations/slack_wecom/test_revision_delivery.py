"""真实队列与原账本的隔离回归；来源校验器和企微网络为替身。"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import relay
from types import SimpleNamespace
import unittest

import notice_format
from scheduled_client import read_result
from resume import assert_reconciled
from scheduled_delivery import ScheduledDelivery
from scheduled_runtime import ScheduledRuntime
import test_scheduled_delivery as baseline
import test_scheduled_inspect as inspection


class RevisionTests(unittest.TestCase):
    setUp = baseline.ScheduledTests.setUp
    tearDown = baseline.ScheduledTests.tearDown
    send = baseline.ScheduledTests.send
    advance = baseline.ScheduledTests.advance
    admit = baseline.ScheduledTests.admit

    def queue(self):
        if not hasattr(self, "plans"):
            self.plans = {self.plan["bundle_sha256"]: copy.deepcopy(self.plan)}
            self.live = self.plan["bundle_sha256"]
        def verify(bundle, commit):
            hashed = hashlib.sha256(bundle).hexdigest()
            if hashed != self.live:
                raise ValueError("superseded_public_pages")
            return copy.deepcopy(self.plans[hashed])
        from test_notice_connector import NOTICE_CONFIG
        return ScheduledDelivery(self.store, NOTICE_CONFIG, verify, self.wecom, lambda: self.now[0], activation_date="2026-11-01")

    def revise(self, revision=2, kind="correction", summary="更正实验条件与结论。", *, admit=True):
        value = json.loads(self.bundle)
        rid = f"morning-20261101-r{revision}"
        value["request_id"] = rid
        for item in value["files"]:
            item["path"] = item["path"].replace("/r1/", "/")
            if item["path"].endswith("/report.json"):
                report = json.loads(base64.b64decode(item["base64"]))
                report.update(schema_version="1.1", revision=revision, revision_notice={"kind": kind, "summary": summary})
                metadata = json.dumps(report).encode()
                item.update(base64=base64.b64encode(metadata).decode(), sha256=hashlib.sha256(metadata).hexdigest())
        bundle = json.dumps(value).encode(); hashed = hashlib.sha256(bundle).hexdigest()
        base = self.q.config["notice_connector"]["report_base_url"]
        manifest = notice_format.build_manifest(report, base, hashlib.sha256(metadata).hexdigest(), "a"*40, stable=True)
        self.assertEqual(notice_format.bundle_poster(bundle, manifest, base), self.poster)
        self.plans[hashed] = {**self.plan, "report_request_id": rid, "bundle_sha256": hashed, "notice_key": manifest["index_url"],
                              "text": manifest["text"], "revision_notice": {"revision": revision, "kind": kind, "summary": summary}}
        self.live = hashed
        if admit:
            self.q.admit(rid, bundle, "a"*40)
        return rid, bundle

    def finish_initial(self):
        self.admit(); self.q.tick(); self.advance()
        self.assertEqual(self.q.status(self.plan["report_request_id"])["phase"], "complete")

    def test_important_revision_one_text_and_idempotent_original_receipt(self):
        self.finish_initial(); rid, bundle = self.revise()
        result = self.advance()
        self.assertEqual((result["status"], result["parts_sent"], result["parts_total"]), ("sent", 1, 1))
        self.assertEqual(result.get("images", []), [])
        self.assertIn("重要更正", self.calls[-1][1]); self.assertEqual([c[0] for c in self.calls], ["text", "image", "text"])
        self.q.admit(rid, bundle, "a"*40); self.q = self.queue(); self.advance(100)
        self.assertEqual(len(self.calls), 3)

    def test_maintenance_after_delivery_is_explicitly_skipped(self):
        self.finish_initial(); rid, _ = self.revise(kind="maintenance", summary="修复排版。")
        result = self.q.status(rid)
        self.assertEqual((result["status"], result["parts_total"], result["delivery_kind"]), ("skipped", 0, "web_only"))
        self.assertIsNone(self.advance()); self.assertEqual(len(self.calls), 2)

    def test_failed_manual_repeat_does_not_forget_completed_original(self):
        from test_notice_connector import NOTICE_CONFIG
        from test_article_notice import message, manifest
        from test_relay import FakeSlack
        self.finish_initial()
        slack = FakeSlack(); slack.messages = [message(2)]
        slack.download_image = lambda plan: b'invalid image bytes'
        receiver = relay.Relay(self.store, slack, self.wecom, NOTICE_CONFIG, clock=lambda:self.now[0],
                               sleeper=lambda seconds:self.now.__setitem__(0,self.now[0]+seconds),
                               notice_reader=SimpleNamespace(read=lambda candidate:manifest()))
        result = receiver.deliver('manual-failed', {'test':'repeat'}, receiver.delivery_text(slack.messages[0]), slack.messages[0]['ts'],
            'manual_slack', 1000, repeat=True, reason='explicit test repeat',
            notice_key=self.plan['notice_key'], image_plans=receiver.image_plans(slack.messages[0]))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['parts_sent'], 0)
        rid, _ = self.revise()
        self.assertEqual(self.q.status(rid)['delivery_kind'], 'revision_update')
        self.assertEqual(self.advance()['status'], 'sent')

    def test_integral_revision_number_preserves_original_bundle_bytes(self):
        _, bundle = self.revise(admit=False)
        value = json.loads(bundle)
        item = next(f for f in value['files'] if f['path'].endswith('/report.json'))
        report = json.loads(base64.b64decode(item['base64'])); report['revision'] = 2.0
        raw = json.dumps(report).encode()
        item.update(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        base = self.q.config['notice_connector']['report_base_url']
        manifest = notice_format.build_manifest({**report, 'revision':2}, base, item['sha256'], 'a'*40, stable=True)
        self.assertEqual(notice_format.bundle_poster(json.dumps(value).encode(), manifest, base), self.poster)

    def test_failed_legacy_initial_with_unattempted_poster_requires_reconciliation(self):
        from test_notice_connector import NOTICE_CONFIG, notice
        from test_relay import FakeSlack
        slack=FakeSlack(); message=notice(2);slack.download_image=lambda plan:self.poster
        receiver=relay.Relay(self.store,slack,self.wecom,NOTICE_CONFIG,clock=lambda:self.now[0],
                             sleeper=lambda seconds:self.now.__setitem__(0,self.now[0]+seconds))
        self.outcomes=['failed']
        result=receiver.deliver('manual-initial',{'test':'initial'},receiver.delivery_text(message),message['ts'],
            'manual_slack',1000,notice_key=receiver.notice_key(message),image_plans=receiver.image_plans(message))
        self.assertEqual((result['status'],result['parts_sent']),('failed',0))
        self.assertEqual(result['images'][0]['status'],'not_attempted')
        self.assertFalse(self.q.revisions.started('manual-initial'))
        for kind in ['correction','maintenance']:
            with self.assertRaisesRegex(ValueError,'legacy_initial_reconciliation_required'):
                self.revise(kind=kind)
        self.assertEqual([c[0] for c in self.calls],['text'])

    def test_unstarted_initial_is_superseded_without_erasing_any_parts(self):
        self.admit(); before = self.q.revisions.parts(self.plan["report_request_id"])
        rid, _ = self.revise(); self.advance(); self.advance()
        old = self.q.status(self.plan["report_request_id"])
        self.assertEqual((old["phase"], old["parts_sent"]), ("superseded", 0))
        self.assertEqual(self.q.revisions.parts(self.plan["report_request_id"]), before)
        self.assertEqual(self.q.status(rid)["delivery_kind"], "initial")
        self.assertEqual([c[0] for c in self.calls], ["text", "image"])
        self.assertEqual(self.q.inspect(self.plan["report_request_id"], "2026-11-01")["scheduler"]["phase"], "superseded")
        assert_reconciled(self.store)
        with self.store.db:
            self.store.db.execute("UPDATE scheduled_supersessions SET original_record_hash=?", ("0"*64,))
        with self.assertRaises(ValueError):
            assert_reconciled(self.store)

    def test_first_revision_owns_the_day_without_inventing_r1(self):
        rid, _ = self.revise(kind="maintenance", summary="发布前排版修正。")
        self.assertEqual(self.q.status(rid)["delivery_kind"], "initial")
        inspection = self.q.inspect("morning-20261101-r1", "2026-11-01")
        self.assertEqual(inspection["scheduler_day_claim"]["request_id"], rid)
        request = json.dumps({"action": "inspect", "request_id": "morning-20261101-r1", "report_date": "2026-11-01"}).encode()
        self.assertEqual(read_result(json.dumps(inspection).encode(), request), inspection)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM requests WHERE id='morning-20261101-r1'").fetchone())
        self.advance(); self.advance(); self.assertEqual(self.q.status(rid)["status"], "sent")

    def test_partial_initial_keeps_original_receipts_and_sends_only_update(self):
        self.admit(); self.q.tick(); rid, _ = self.revise(); self.advance()
        old = self.q.status(self.plan["report_request_id"])
        self.assertEqual((old["phase"], old["parts_sent"], old["parts_total"]), ("superseded", 1, 2))
        self.assertEqual([c[0] for c in self.calls], ["text", "text"])
        self.assertEqual(self.q.status(rid)["parts_total"], 1)

    def test_maintenance_carries_unsent_important_update(self):
        self.finish_initial(); first, _ = self.revise()
        last, _ = self.revise(3, "maintenance", "修复页面排版。")
        self.assertEqual(self.q.status(first)["phase"], "superseded")
        self.assertEqual(self.q.status(last)["covered_requests"], [first])
        self.advance(); self.assertIn("更正实验条件与结论", self.calls[-1][1])
        self.assertEqual(self.q.status(last)["delivery_kind"], "revision_update")

    def test_multiple_revisions_before_first_send_keep_one_complete_initial(self):
        self.admit(); second, _ = self.revise(); third, _ = self.revise(3, "maintenance", "调整排版。")
        self.assertEqual(self.q.status(second)["phase"], "superseded")
        self.advance(); self.advance(); self.assertEqual([c[0] for c in self.calls], ["text", "image"])
        self.assertEqual(self.q.status(third)["parts_sent"], 2)
        assert_reconciled(self.store)

    def test_unknown_revision_is_frozen_across_restart_and_other_days(self):
        self.finish_initial(); rid, _ = self.revise(); self.outcomes = [TimeoutError()]
        self.advance(); self.store.db.close(); self.store = relay.Store(Path(self.tmp.name)); self.q = self.queue()
        self.assertEqual(self.q.status(rid)["phase"], "unknown")
        ScheduledRuntime.assert_reconciled(SimpleNamespace(queue=self.q))
        self.assertIsNone(self.q.inspect("morning-20261102-r1", "2026-11-02")["scheduler"])
        self.assertIsNone(self.advance(10000)); self.assertEqual(len(self.calls), 3)
        duplicate, _ = self.revise(3)
        self.assertEqual(self.q.status(duplicate)["status"], "skipped")

    def test_supersession_fences_a_late_verifier_before_sending(self):
        self.finish_initial(); first, _ = self.revise(); verify = self.q.verify; triggered = False
        def delayed(bundle, commit):
            nonlocal triggered
            result = verify(bundle, commit)
            if not triggered:
                triggered = True
                self.revise(3, "material_update", "补充新的实验结论并保留原更正。")
            return result
        self.q.verify = delayed
        self.advance(); self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.q.status(first)["phase"], "superseded")
        self.advance(); self.assertEqual(len(self.calls), 3)

    def test_explicit_failure_retries_same_text_and_changed_binding_is_rejected(self):
        self.finish_initial(); rid, _ = self.revise(); self.outcomes = ["failed", "sent"]
        self.advance(); self.assertEqual(self.q.status(rid)["attempts"], 1)
        self.assertIsNone(self.advance()); self.advance(16)
        self.assertEqual(self.calls[-1], self.calls[-2]); self.assertEqual(self.q.status(rid)["status"], "sent")
        with self.store.db:
            self.store.db.execute("UPDATE parts SET content_hash=? WHERE request_id=?", ("0"*64, rid))
        with self.assertRaisesRegex(ValueError, "binding"):
            self.q.revisions.check_record(rid)

    def test_orphan_unknown_still_blocks_daily_absence(self):
        self.finish_initial(); self.revise()
        with self.store.db:
            self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('orphan','x','manual_text',1,'unknown')")
        with self.assertRaises(ValueError):
            self.q.inspect("morning-20261102-r1", "2026-11-02")

    def test_corrupt_unknown_status_cannot_pass_startup_or_next_day_inspection(self):
        self.finish_initial(); rid, _ = self.revise(); self.outcomes = [TimeoutError()]; self.advance()
        with self.store.db:
            self.store.db.execute("UPDATE requests SET status='queued' WHERE id=?", (rid,))
        with self.assertRaises(ValueError):
            ScheduledRuntime.assert_reconciled(SimpleNamespace(queue=self.q))
        with self.assertRaises(ValueError):
            self.q.inspect("morning-20261102-r1", "2026-11-02")

    def test_new_important_revision_after_confirmed_send_is_a_distinct_notice(self):
        self.finish_initial(); self.revise(); self.advance(); rid, _ = self.revise(3)
        self.assertEqual(self.q.status(rid)["delivery_kind"], "revision_update")
        self.advance(); self.assertEqual(len(self.calls), 4)

    def test_maintenance_inherits_failed_notification_deadline_and_attempts(self):
        self.finish_initial(); first, _ = self.revise(); self.outcomes = ['failed', 'sent']; self.advance()
        original = self.q.status(first)
        latest, _ = self.revise(3, 'maintenance', '修复页面排版。')
        result = self.q.status(latest)
        self.assertEqual((result['attempts'], result['next_attempt_at']), (original['attempts'], original['next_attempt_at']))
        self.assertIsNone(self.advance()); self.assertEqual(len(self.calls), 3)
        self.advance(16); self.assertEqual(self.calls[-1], self.calls[-2])

    def test_manual_repeat_cannot_replace_unfinished_revision_initial(self):
        from test_notice_connector import NOTICE_CONFIG
        rid, _ = self.revise()
        receiver = relay.Relay(self.store, None, self.wecom, NOTICE_CONFIG, clock=lambda:self.now[0])
        before = self.store.db.total_changes
        with self.assertRaisesRegex(relay.RelayError, 'scheduled_reconciliation_required'):
            receiver.deliver('manual-unfinished', {'test':'repeat'}, 'text', '1793491202.000001', 'manual_slack', 1000,
                             repeat=True, reason='explicit test repeat', notice_key=NOTICE_CONFIG['notice_connector']['report_base_url']+'2026-11-01/r2/')
        self.assertEqual(self.store.db.total_changes, before); self.assertEqual(self.calls, [])

    def test_completed_revision_manual_handoff_preserves_next_day_inspection(self):
        self.revise(); self.advance(); self.advance()
        inspection.InspectionTests.manual_successor(self)
        self.assertIsNone(self.q.inspect('morning-20261102-r1','2026-11-02')['scheduler'])
        self.assertEqual(len(self.calls),4)

    def test_superseded_revision_manual_handoff_preserves_next_day_inspection(self):
        self.revise(); self.revise(3, 'maintenance', '修复排版。'); self.advance(); self.advance()
        inspection.InspectionTests.manual_successor(self)
        self.assertIsNone(self.q.inspect('morning-20261102-r1','2026-11-02')['scheduler'])

    def test_superseded_original_and_revision_manual_handoff_preserves_next_day(self):
        self.admit(); self.revise(); self.advance(); self.advance()
        inspection.InspectionTests.manual_successor(self,revision=2)
        self.assertIsNone(self.q.inspect('morning-20261102-r1','2026-11-02')['scheduler'])
        self.assertEqual(self.q.status(self.plan['report_request_id'])['phase'],'superseded')


if __name__ == "__main__":
    unittest.main()
