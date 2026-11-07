"""离线故障测试，不安装服务，不调用真实发送接口。"""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

import relay
from scheduled_delivery import ScheduledDelivery, ScheduledDeliveryError, reconcile_daily_claims
from test_notice_bundle import fixture
from test_notice_connector import NOTICE_CONFIG


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = relay.Store(Path(self.tmp.name))
        self.bundle,self.poster,manifest,_ = fixture()
        self.now = [1793491200.0]  # 合成时间：2026-11-01 00:00 UTC
        self.calls=[]
        self.plan={"report_request_id":"isolated-bundle-fixture","report_date":"2026-11-01","report_commit":"a"*40,
            "bundle_sha256":hashlib.sha256(self.bundle).hexdigest(),"notice_key":manifest["index_url"],
            "text":manifest["text"],"poster":self.poster,"poster_sha256":hashlib.sha256(self.poster).hexdigest(),"mime":"image/png"}
        self.outcomes=[]
        class WeCom:
            def send(_, text): return self.send("text",text)
            def send_image(_, image,mime): return self.send("image",image)
        self.wecom=WeCom()
        self.q=self.queue()
    def queue(self):
        return ScheduledDelivery(self.store,NOTICE_CONFIG,lambda b,c:copy.deepcopy(self.plan),self.wecom,lambda:self.now[0],activation_date="2026-11-01")
    def send(self,kind,data):
        self.calls.append((kind,data))
        outcome=self.outcomes.pop(0) if self.outcomes else "sent"
        if isinstance(outcome,Exception): raise outcome
        return outcome
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def admit(self): return self.q.admit("isolated-bundle-fixture",self.bundle,"a"*40)
    def advance(self,seconds=4): self.now[0]+=seconds;return self.q.tick()
    def test_due_and_exact_original_bytes(self):
        self.now[0]-=1;self.admit();self.assertIsNone(self.q.tick());self.assertEqual(self.calls,[])
        self.advance();self.advance();self.advance();result=self.q.status("isolated-bundle-fixture")
        self.assertEqual(result["status"],"sent");self.assertEqual(result["parts_sent"],result["parts_total"])
        self.assertEqual(self.calls,[("text",self.plan["text"]),("image",self.poster)])
        self.assertIsNone(self.q.tick())
    def test_duplicate_and_conflict(self):
        self.admit();self.admit()
        with self.assertRaises(ScheduledDeliveryError):self.q.admit("other",self.bundle,"a"*40)
        self.plan["text"]+="different"
        with self.assertRaises(ScheduledDeliveryError):self.admit()
    def test_known_failure_retries_only_failed_part(self):
        self.admit();self.q.tick();self.outcomes=["failed","sent"]
        self.advance();result=self.q.status("isolated-bundle-fixture");self.assertEqual(result["attempts"],1)
        self.assertIsNone(self.q.tick());self.advance(16);self.advance();result=self.q.status("isolated-bundle-fixture")
        self.assertEqual(result["status"],"sent")
        self.assertEqual([k for k,_ in self.calls],["text","image","image"])
    def test_unknown_never_retried_by_another_executor(self):
        self.admit();self.outcomes=[TimeoutError()];result=self.q.tick()
        self.assertEqual(result["phase"],"unknown")
        self.q=self.queue();self.assertIsNone(self.advance(9999999));self.assertEqual(len(self.calls),1)
    def test_crash_after_intent_is_not_a_new_send(self):
        self.admit()
        with self.store.db:self.store.db.execute("UPDATE scheduled_notices SET phase='sending'")
        self.q=self.queue();self.assertIsNone(self.advance(9999999));self.assertEqual(self.calls,[])
    def test_two_executors_and_read_only_status(self):
        self.admit();other=self.queue();before=self.store.db.total_changes
        other.status("isolated-bundle-fixture");other.status("absent");self.assertEqual(before,self.store.db.total_changes)
        self.q.tick();self.assertIsNone(other.tick());self.assertEqual(len(self.calls),1)
    def test_live_tampering_prevents_send(self):
        self.admit();self.plan["text"]+="tamper";self.q.tick();self.assertEqual(self.calls,[])
        self.assertEqual(self.q.status("isolated-bundle-fixture")["attempts"],1)
    def test_legacy_edition_blocks_new_admission(self):
        key="notice_edition:"+relay.digest([NOTICE_CONFIG["team_id"],NOTICE_CONFIG["channel_id"],self.plan["notice_key"]])
        self.store.set(key,"old-request")
        with self.assertRaises(ScheduledDeliveryError):self.admit()
    def test_expired_verification_cannot_send(self):
        self.admit()
        def slow(b,c):self.now[0]+=61;return copy.deepcopy(self.plan)
        self.q.verify=slow
        for attempt in range(1,4):
            result=self.q.tick()
            self.assertEqual(self.calls,[])
            self.assertEqual(result["phase"],"pending")
            self.assertEqual(result["attempts"],attempt)
            self.assertEqual(result["last_error"],"verification_expired")
            self.assertEqual(result["next_attempt_at"],self.now[0]+15*(2**(attempt-1)))
            self.assertIsNone(self.q.tick())
            self.now[0]=result["next_attempt_at"]
        self.q.verify=lambda b,c:copy.deepcopy(self.plan)
        self.q.tick();self.advance()
        self.assertEqual(self.q.status("isolated-bundle-fixture")["status"],"sent")
        self.assertEqual([kind for kind,_ in self.calls],["text","image"])
    def test_restart_after_known_success_does_not_repeat_text(self):
        self.admit();self.q.tick();self.store.db.close();self.store=relay.Store(Path(self.tmp.name));self.q=self.queue();self.advance();self.advance();result=self.q.status("isolated-bundle-fixture")
        self.assertEqual(result["status"],"sent");self.assertEqual([k for k,_ in self.calls],["text","image"])

    def test_missing_text_ledger_cannot_skip_to_image(self):
        self.admit()
        with self.store.db:self.store.db.execute("DELETE FROM parts WHERE request_id='isolated-bundle-fixture'")
        with self.assertRaises(ScheduledDeliveryError):self.q.tick()
        self.assertEqual(self.calls,[])
    def test_concurrent_verifier_cannot_take_live_lease(self):
        self.admit();other=self.queue()
        def verify(b,c):
            self.assertIsNone(other.tick())
            return copy.deepcopy(self.plan)
        self.q.verify=verify;self.q.tick();self.assertEqual(len(self.calls),1)
    def test_known_failure_has_no_total_retry_cutoff(self):
        self.admit()
        for attempt in range(20):
            self.outcomes=["failed"];self.q.tick()
            self.now[0]=self.q.status("isolated-bundle-fixture")["next_attempt_at"]
        self.assertEqual(self.q.status("isolated-bundle-fixture")["attempts"],20)
        self.q.tick();self.advance();self.advance();result=self.q.status("isolated-bundle-fixture")
        self.assertEqual(result["status"],"sent")
    def test_original_bundle_and_poster_are_not_reencoded(self):
        self.admit()
        row=self.store.db.execute("SELECT bundle,poster FROM scheduled_notices").fetchone()
        self.assertEqual(row,(self.bundle,self.poster))

    def test_final_sent_is_committed_without_further_network(self):
        self.admit();self.q.tick();self.advance()
        def offline(*args):raise OSError("offline after final success")
        self.q.verify=offline
        self.assertEqual(self.q.status("isolated-bundle-fixture")["phase"],"complete")
        self.assertIsNone(self.advance());self.assertEqual(len(self.calls),2)

    def legacy_claim(self, revision=2):
        plan=copy.deepcopy(self.plan);plan["notice_key"]=plan["notice_key"].replace("/r1/",f"/r{revision}/")
        key="notice_edition:"+relay.digest([NOTICE_CONFIG["team_id"],NOTICE_CONFIG["channel_id"],plan["notice_key"]])
        with self.store.db:
            self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('legacy','hash','auto',1,'sent')")
            self.store.db.execute("INSERT INTO meta VALUES (?,?)",(key,"legacy"))
        return plan
    def test_legacy_cross_revision_requires_verified_migration_then_blocks(self):
        old=self.legacy_claim()
        with self.assertRaises(ScheduledDeliveryError):self.queue()
        with self.assertRaises(ScheduledDeliveryError):reconcile_daily_claims(self.store,NOTICE_CONFIG,lambda b,c:old,[])
        reconcile_daily_claims(self.store,NOTICE_CONFIG,lambda b,c:old,[(self.bundle,"a"*40)])
        self.q=self.queue()
        with self.assertRaises(ScheduledDeliveryError):self.admit()
        self.assertEqual(self.store.status("legacy")["status"],"sent")
    def test_new_day_claim_stops_legacy_different_revision(self):
        self.admit()
        receiver=relay.Relay(self.store,None,self.wecom,NOTICE_CONFIG,clock=lambda:self.now[0])
        result=receiver.deliver("legacy-r2",{},"must not send","1793491200.000001","auto",1,
                                notice_key=self.plan["notice_key"].replace("/r1/","/r2/"))
        self.assertEqual(result["status"],"source_already_attempted");self.assertEqual(self.calls,[])

    def test_original_65_and_80_character_request_ids_are_supported(self):
        import json
        import scheduled_socket
        for length in (65,80):
            with self.subTest(length=length):
                directory=Path(self.tmp.name)/str(length);directory.mkdir(mode=0o700)
                store=relay.Store(directory);plan=copy.deepcopy(self.plan);request_id="a"*length;plan["report_request_id"]=request_id
                try:
                    queue=ScheduledDelivery(store,NOTICE_CONFIG,lambda b,c:plan,self.wecom,lambda:self.now[0],activation_date="2026-11-01")
                    self.assertEqual(queue.admit(request_id,self.bundle,"a"*40)["request_id"],request_id)
                    result=scheduled_socket.dispatch(json.dumps({"action":"status","request_id":request_id}).encode(),queue)
                    self.assertEqual(result["status"],"queued")
                    with self.assertRaises(ScheduledDeliveryError):queue.admit("a"*81,self.bundle,"a"*40)
                finally:store.db.close()

if __name__=="__main__":unittest.main()
