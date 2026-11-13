"""合成验证计划，不进行外部发布或发送。"""
import copy
import hashlib
import json
import unittest
import relay
from page_delivery import TOPICS, notice_text, validate_pages, validate_progress
from test_scheduled_delivery import ScheduledTests
from scheduled_delivery import ScheduledDelivery
from test_notice_connector import NOTICE_CONFIG


class PageDeliveryTests(unittest.TestCase):
    setUp = ScheduledTests.setUp
    tearDown = ScheduledTests.tearDown
    send = ScheduledTests.send
    advance = ScheduledTests.advance

    def queue(self):
        if not hasattr(self, 'plans'):
            self.plans = {}
        return ScheduledDelivery(self.store, NOTICE_CONFIG, lambda b, c: copy.deepcopy(self.plans[hashlib.sha256(b).hexdigest()]), self.wecom, lambda: self.now[0], activation_date='2026-11-01')

    def make(self, n=2, revision=1, previous=None):
        rid = f'morning-20261101-r{revision}'
        articles = [{'id': i, 'title': i, 'sha256': 'a'*64} for i in TOPICS[:n]]
        state = {'articles': articles, 'previous': previous, 'added': list(TOPICS[:n] if revision == 1 else TOPICS[n-1:n]), 'pending': [{'id': i, 'reason': '正文待核验'} for i in TOPICS[n:]]}
        bundle = json.dumps({'synthetic':rid, 'state':state}).encode()
        plan = {**self.plan, 'report_request_id':rid, 'notice_key':NOTICE_CONFIG['notice_connector']['report_base_url']+'2026-11-01/', 'bundle_sha256': hashlib.sha256(bundle).hexdigest(), 'page_state':state}
        if revision > 1:
            plan['revision_notice'] = {'revision':revision, 'kind':'page_completion', 'summary':'补齐新页'}
        plan['text'] = notice_text('2026-11-01', [{**a, 'url':plan['notice_key']+a['id']+'/'} for a in articles],state)
        self.plans[plan['bundle_sha256']] = plan
        return rid,bundle,plan

    def first(self, complete=True):
        rid,bundle,plan=self.make()
        self.q.admit(rid,bundle,'a'*40)
        if complete:
            self.q.tick();self.advance()
        return {'request_id':rid,'bundle_sha256':plan['bundle_sha256']}

    def test_first_explains_gap_then_only_added_page_sent(self):
        previous=self.first()
        self.assertEqual(self.q.status(previous['request_id'])['page_state']['pending'][0]['id'],TOPICS[2])
        rid,bundle,plan=self.make(3,2,previous)
        self.q.admit(rid,bundle,'a'*40);result=self.advance()
        self.assertEqual(result['phase'],'complete')
        self.assertEqual(result['delivery_kind'],'page_completion')
        self.assertEqual(result['images'],[])
        self.assertEqual([k for k,_ in self.calls],['text','image','text'])
        self.assertNotIn('/model-serving-gpu/',self.calls[-1][1])
        self.assertIn('/model-architecture-status/',self.calls[-1][1])
        self.q=self.queue();self.q.admit(rid,bundle,'a'*40);self.advance(3600)
        self.assertEqual(len(self.calls),3)

    def test_pending_or_unknown_original_cannot_be_replayed_as_supplement(self):
        previous=self.first(False)
        rid,bundle,_=self.make(3,2,previous)
        with self.assertRaisesRegex(ValueError,'page_predecessor_unresolved'):self.q.admit(rid,bundle,'a'*40)
        self.outcomes=['unknown'];self.q.tick()
        with self.assertRaisesRegex(ValueError,'page_predecessor_unresolved'):self.q.admit(rid,bundle,'a'*40)
        self.assertEqual(len(self.calls),1)

    def test_unknown_supplement_blocks_next_revision(self):
        previous=self.first();rid,bundle,plan=self.make(3,2,previous)
        self.q.admit(rid,bundle,'a'*40);self.outcomes=['unknown'];self.advance()
        r3,b3,_=self.make(4,3,{'request_id':rid,'bundle_sha256':plan['bundle_sha256']})
        with self.assertRaisesRegex(ValueError,'page_predecessor_unresolved'):self.q.admit(r3,b3,'a'*40)
        self.q=self.queue();self.advance(3600);self.assertEqual(len(self.calls),3)

    def test_wrong_hash_or_changed_old_page_rejected(self):
        previous=self.first();rid,bundle,plan=self.make(3,2,previous)
        for mutation in ('hash','old'):
            changed=copy.deepcopy(plan)
            if mutation=='hash':changed['page_state']['previous']['bundle_sha256']='b'*64
            else:changed['page_state']['articles'][0]['sha256']='b'*64
            self.plans[plan['bundle_sha256']]=changed
            with self.assertRaisesRegex(ValueError,'invalid_page_delivery'):self.q.admit(rid,bundle,'a'*40)
        self.assertEqual(len(self.calls),2)

    def test_failed_supplement_resumes_same_part_after_restart(self):
        previous=self.first();rid,bundle,_=self.make(3,2,previous)
        self.q.admit(rid,bundle,'a'*40);self.outcomes=['failed'];self.advance()
        self.assertEqual(self.q.status(rid)['status'],'failed')
        self.q=self.queue();self.advance(3600)
        self.assertEqual(self.q.status(rid)['phase'],'complete')
        self.assertEqual([kind for kind,_ in self.calls],['text','image','text','text'])
        self.assertEqual(self.calls[-1][1],self.calls[-2][1])
        self.assertNotIn('/model-serving-gpu/',self.calls[-1][1])

    def test_correction_and_web_only_maintenance_preserve_pending_then_complete(self):
        previous=self.first()
        for revision,kind in ((2,'correction'),(3,'maintenance')):
            rid,bundle,plan=self.make(2,revision,previous)
            plan['page_state']['added']=[]
            plan['page_state']['articles'][0]['sha256']='b'*64
            plan['revision_notice']['kind']=kind
            plan['text']=notice_text(plan['report_date'],[{**a,'url':plan['notice_key']+a['id']+'/'} for a in plan['page_state']['articles']],plan['page_state'])
            self.q.admit(rid,bundle,'a'*40);self.advance()
            self.q=self.queue()
            self.q.admit(rid,bundle,'a'*40)
            previous={'request_id':rid,'bundle_sha256':plan['bundle_sha256']}
        self.assertEqual([k for k,_ in self.calls],['text','image','text'])
        self.assertIn('重要更正',self.calls[-1][1])
        rid,bundle,plan=self.make(3,4,previous)
        plan['page_state']['articles'][0]['sha256']='b'*64
        self.q.admit(rid,bundle,'a'*40);self.advance()
        self.assertEqual(len(self.calls),4)
        self.assertIn('/model-architecture-status/',self.calls[-1][1])
        self.assertNotIn('/model-serving-gpu/',self.calls[-1][1])

    def test_complete_chain_no_duplicate_pages(self):
        previous=self.first()
        for n in range(3,7):
            rid,bundle,plan=self.make(n,n-1,previous)
            self.q.admit(rid,bundle,'a'*40);self.advance()
            previous={'request_id':rid,'bundle_sha256':plan['bundle_sha256']}
        self.assertEqual(self.q.status(rid)['page_state']['pending'],[])
        self.assertEqual(len(self.calls),6)

class PageBundleTests(unittest.TestCase):
    def test_sixth_page_budget_counts_only_newly_sent_text(self):
        import notice_format
        from test_notice_connector import PROFILE
        report=dict(schema_version="1.2",date="2026-11-01",revision=6,articles=[dict(id=i,title="A"*250) for i in TOPICS],poster="assets/poster.png",images=[dict(path="assets/poster.png",sha256="a"*64)])
        report["page_delivery"]=dict(previous=dict(request_id="morning-20261101-r5",bundle_sha256="b"*64),added=[TOPICS[-1]],pending=[])
        manifest=notice_format.build_manifest(report,PROFILE["report_base_url"],"c"*64,"d"*40,stable=True)
        notice_format.validate_manifest(manifest,PROFILE["report_base_url"])
        self.assertLess(len(manifest["text"].encode()),500)
        report["articles"][0]["title"]="A"*301
        with self.assertRaises(ValueError):notice_format.build_manifest(report,PROFILE["report_base_url"],"c"*64,"d"*40,stable=True)

    def test_report_manifest_and_raw_bundle_binding(self):
        import base64
        import notice_format
        from test_notice_bundle import fixture
        from test_notice_connector import PROFILE
        data,poster,_,_=fixture()
        bundle=json.loads(data)
        for item in bundle['files']:
            item['path']=item['path'].replace('/r1/','/').replace('articles/memory.md','articles/model-serving-gpu.md')
            if item['path'].endswith('report.json'):
                report=json.loads(base64.b64decode(item['base64']))
                report['schema_version']='1.2';report['articles'][0]['id']=TOPICS[0]
                report['page_delivery']={'previous':None,'added':[TOPICS[0]],'pending':[{'id':i,'reason':'正文仍待核验'} for i in TOPICS[1:]]}
                raw=json.dumps(report).encode();item['base64']=base64.b64encode(raw).decode();item['sha256']=hashlib.sha256(raw).hexdigest()
        manifest=notice_format.build_manifest(report,PROFILE['report_base_url'],hashlib.sha256(raw).hexdigest(),'a'*40,stable=True)
        self.assertEqual(notice_format.bundle_poster(json.dumps(bundle).encode(),manifest,PROFILE['report_base_url']),poster)
        tampered=copy.deepcopy(manifest);tampered['page_delivery']['pending'].pop()
        with self.assertRaises(ValueError):notice_format.validate_manifest(tampered,PROFILE['report_base_url'])
