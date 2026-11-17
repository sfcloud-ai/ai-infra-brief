"""离线合成合同回归；不作为真实检索或性能验证。"""
from copy import deepcopy
import base64
from datetime import timedelta
import hashlib
import json
import unittest
from urllib.parse import urlencode

from ai_brief import discovery
from ai_brief.contracts import RESOURCE_ROOT, parse_time
from tests import test_layered_budget as layered


class CommunityDiscoveryTests(unittest.TestCase):
    save = layered.LayeredBudgetTests.save
    check = layered.LayeredBudgetTests.check
    reject = layered.LayeredBudgetTests.reject
    artifact = layered.LayeredBudgetTests.artifact

    def setUp(self):
        layered.LayeredBudgetTests.setUp(self)
        self.audit['coverage_contract'] = discovery.CONTRACT
        raw = (RESOURCE_ROOT / 'prompts/audit.md').read_bytes()
        self.root.joinpath('prompts/audit.md').write_bytes(raw)
        self.audit['inputs']['prompt_hashes']['audit.md'] = hashlib.sha256(raw).hexdigest()
        self.audit['discovery'] = {'queries': [], 'backscan': {'status': 'budget_deferred', 'reason': 'Synthetic bounded budget'},
                                   'followup': {'status': 'none_due', 'reason': 'Synthetic empty followup list'}}
        samples = [
            ('discovery_memory', 'GPU memory oversubscription offload in:readme', 'HAMi-project/HAMi'),
            ('discovery_scheduling', 'GPU sharing scheduler isolation in:readme', 'mit-gfx/Salus'),
            ('discovery_compute', 'GPU tensor throughput FP32 accumulation in:readme', 'JustAResearcher/gpu-unlock-ada'),
            ('discovery_data_path', 'GPU communication RDMA transfer in:readme', 'microsoft/mscclpp'),
        ]
        for family, query, repo in samples:
            self.add_query(family, query, repo)

    def response(self, query, value):
        data = json.dumps(value).encode()
        source = next(s for s in self.audit['sources'] if s['id'] == query['source_id'])
        self.root.joinpath('evidence', source['id'] + '.txt').write_bytes(data)
        source['content_sha256'] = hashlib.sha256(data).hexdigest()
        query['response_sha256'] = source['content_sha256']

    def add_query(self, family, query, repo, mode='current'):
        identity = 'search-' + str(len(self.audit['discovery']['queries']))
        url = 'https://github.com/' + repo
        source = deepcopy(self.audit['sources'][1])
        source.update(id=identity, url='https://api.github.com/search/repositories?' + urlencode({'q': query, 'per_page': 10, 'page': 1}))
        self.audit['sources'].append(source)
        q = {'id': identity, 'family': family, 'mode': mode, 'provider': 'github_search', 'query': query,
             'since': (parse_time(self.audit['as_of']) - timedelta(hours=24)).isoformat(), 'until': self.audit['as_of'],
             'source_id': identity, 'result_limit': 10, 'page': 1, 'truncated': False, 'followup_of': None,
             'attempt': 1, 'response_sha256': None,
             'results': [{'url': url, 'repository_url': url, 'disposition': 'watch', 'novelty': 'old_discovery',
                          'source_ids': [], 'event_key': None, 'reason': 'Synthetic old project, no new event verified',
                          'next_step': 'Check later maintainer benchmark and implementation changes', 'baseline_url': None}]}
        self.audit['discovery']['queries'].append(q)
        if mode == 'current':
            self.audit['checks'][family] = {'status': 'no_qualifying_items', 'source_ids': [identity], 'reason': 'Only this bounded synthetic result set was assessed'}
        self.response(q, {'total_count': 1, 'incomplete_results': False, 'items': [{'html_url': url}]})
        return q

    def test_four_problem_families_are_separate_from_fixed_sources(self):
        result = self.check()
        self.assertEqual(result['readiness'], 'ready')
        self.assertEqual(len(result['required_checks']), 23)
        self.assertNotIn('discovery', result)
        for q in self.audit['discovery']['queries']:
            self.assertNotIn(q['results'][0]['repository_url'].split('/')[-1].casefold(), q['query'].casefold())
        self.assertEqual(result['limits'], self.audit['limits'])

    def test_new_frozen_prompt_cannot_downgrade_or_omit_discovery(self):
        self.audit['coverage_contract'] = 'daily_required_v1'
        self.audit.pop('discovery')
        for key in discovery.CHECKS:
            self.audit['checks'].pop(key)
        self.reject('original frozen audit prompt')

    def test_arbitrary_github_page_is_not_a_search(self):
        q = self.audit['discovery']['queries'][0]
        next(s for s in self.audit['sources'] if s['id'] == q['source_id'])['url'] = q['results'][0]['url']
        self.reject('actual request parameters')

    def test_response_results_not_incidental_urls(self):
        q = self.audit['discovery']['queries'][0]
        self.response(q, {'message': q['query'] + ' ' + q['results'][0]['url']})
        self.reject('structured bounded GitHub search response')

    def test_each_returned_result_needs_a_decision_and_correct_project(self):
        q = self.audit['discovery']['queries'][0]
        old = deepcopy(q['results'])
        q['results'] = []
        self.reject('Every returned discovery result')
        q['results'] = old
        q['results'][0]['repository_url'] = 'https://github.com/unrelated/project'
        self.reject('repository differ')

    def test_owner_restrictions_cannot_fill_broad_discovery(self):
        q = self.audit['discovery']['queries'][0]
        for value in ['GPU ORG:Known', 'GPU "repo:Known/Repo"', 'GPU owner%3AKnown', 'site:github.com/Known GPU']:
            q['query'] = value
            self.reject('across repositories')

    def test_pagination_cannot_fill_a_different_discovery_family(self):
        q = self.audit['discovery']['queries'][2]
        q.update(query=self.audit['discovery']['queries'][0]['query'], page=2, results=[], truncated=True)
        source = next(s for s in self.audit['sources'] if s['id'] == q['source_id'])
        source['url'] = 'https://api.github.com/search/repositories?' + urlencode({'q': q['query'], 'page': 2, 'per_page': 10})
        self.response(q, {'total_count': 1, 'incomplete_results': False, 'items': []})
        self.reject('multiple discovery families')

    def test_successful_reread_retains_every_response_and_result_decision(self):
        old = self.audit['discovery']['queries'][0]
        new = self.retry_query(old)
        new['results'] = []
        self.assertEqual(self.check()['readiness'], 'ready')
        self.audit['discovery']['queries'].remove(old)
        self.reject('Every successful discovery attempt')
        self.audit['discovery']['queries'].insert(0, old)
        self.root.joinpath('evidence', old['response_sha256'] + '.txt').unlink()
        self.reject('Missing audit file')

    def test_real_empty_response_and_bounded_truncation(self):
        q = self.audit['discovery']['queries'][0]
        original = deepcopy(q['results'])
        q['results'] = []
        self.response(q, {'total_count': 0, 'incomplete_results': False, 'items': []})
        self.assertEqual(self.check()['readiness'], 'ready')
        self.response(q, {'total_count': 100, 'incomplete_results': False, 'items': []})
        self.reject('contradicts')
        q['results'] = original
        self.response(q, {'total_count': 100, 'incomplete_results': False, 'items': [{'html_url': original[0]['url']}]})
        self.reject('truncation')
        q['truncated'] = True
        self.assertEqual(self.check()['readiness'], 'ready')
        self.response(q, {'total_count': 100, 'incomplete_results': True, 'items': []})
        q['results'] = []
        self.reject('complete bounded search results')
        self.audit['checks'][q['family']].update(status='failed', failure_kind='incomplete_listing')
        from tests.test_page_scope import PageScopeTests
        PageScopeTests.notice(self)
        self.assertEqual(self.check()['readiness'], 'partial')

    def test_unexecuted_and_failed_queries_are_not_empty_success(self):
        q = self.audit['discovery']['queries'].pop(0)
        check = self.audit['checks'][q['family']]
        check.update(status='not_checked', source_ids=[])
        result = self.check()
        self.assertIn('required_coverage_not_checked', result['blockers'])
        self.audit['discovery']['queries'].insert(0, q)
        check.update(status='no_qualifying_items', source_ids=[q['source_id']])
        source = next(s for s in self.audit['sources'] if s['id'] == q['source_id'])
        source.update(status='failed', content_sha256=None, error='Synthetic 403')
        source['attempt_records'][0].update(status='failed', error='Synthetic 403')
        q['response_sha256'] = None
        q['results'] = []
        self.reject('successful source read')

    def test_old_project_needs_new_event_and_primary_evidence(self):
        q = self.audit['discovery']['queries'][2]
        r = q['results'][0]
        r.update(disposition='selected', event_key='cache-fix')
        self.reject("today's news")
        r['novelty'] = 'new_event'
        self.reject('project primary evidence')
        primary = r['repository_url'] + '/pull/123'
        self.audit['sources'][0]['url'] = self.candidate['source_url'] = primary
        self.draft['entries'][0]['source_urls'] = [primary]
        r['source_ids'] = ['source']
        self.audit['checks'][q['family']]['status'] = 'checked'
        self.assertEqual(self.check()['readiness'], 'ready')
        r['novelty'] = 'new_evidence'
        self.reject('previous baseline')

    def test_followup_and_backscan_keep_real_provenance_and_limits(self):
        q = self.add_query('discovery_compute', 'repo:JustAResearcher/gpu-unlock-ada benchmark', 'JustAResearcher/gpu-unlock-ada', 'followup')
        self.audit['discovery']['followup']['status'] = 'performed'
        self.reject('retained discovery')
        q['followup_of'] = self.audit['discovery']['queries'][2]['results'][0]['url']
        self.assertEqual(self.check()['readiness'], 'ready')
        q['mode'] = 'backscan'
        self.reject('across repositories')

    def test_rss_results_are_structural_and_html_cannot_pass(self):
        q = self.audit['discovery']['queries'][0]
        source = next(s for s in self.audit['sources'] if s['id'] == q['source_id'])
        q.update(provider='web_search', truncated=True, query='site:github.com ' + q['query'])
        source['url'] = 'https://www.bing.com/search?' + urlencode({'q': q['query'], 'format': 'rss', 'count': 10})
        raw = ('<rss><channel><title>Bing: ' + q['query'] + '</title><item><link>' + q['results'][0]['url'] + '</link></item></channel></rss>').encode()
        self.root.joinpath('evidence', source['id'] + '.txt').write_bytes(raw)
        source['content_sha256'] = hashlib.sha256(raw).hexdigest()
        q['response_sha256'] = source['content_sha256']
        self.assertEqual(self.check()['readiness'], 'ready')
        self.response(q, {'html': q['query'] + q['results'][0]['url']})
        self.reject('structured discovery response')

    def partial_page(self):
        from tests.test_page_scope import PageScopeTests
        from ai_brief.research_audit import PAGE_CHECKS
        self.notice = lambda: PageScopeTests.notice(self)
        query = self.audit['discovery']['queries'].pop()
        self.audit['sources'] = [s for s in self.audit['sources'] if s['id'] != query['source_id']]
        ready = ['model-serving-gpu']
        delivery = {'previous': None, 'added': ready, 'pending': [{'id': p, 'reason': 'Synthetic pending evidence'} for p in PAGE_CHECKS if p not in ready]}
        self.audit['page_scope'] = {'contract': 'verified_pages_v1', 'page_delivery': delivery, 'checked_pages': ready, 'previous_evidence': None}
        self.draft.update(schema_version='1.2', articles=[{'id': ready[0], 'title': 'Synthetic verified page'}], page_delivery=deepcopy(delivery))
        self.draft['entries'][0]['article_id'] = ready[0]
        pending = [c for p in delivery['pending'] for c in PAGE_CHECKS[p['id']]] + ['discovery_data_path']
        for name in pending:
            self.audit['checks'][name] = {'status': 'not_checked', 'source_ids': [], 'reason': 'Not yet queried'}
        self.notice()

    def test_pending_discovery_is_honest_and_ready_page_cannot_skip_it(self):
        self.partial_page()
        result = self.check()
        self.assertEqual(result['readiness'], 'partial')
        self.assertIn('discovery_data_path', result['coverage_unchecked'])
        self.audit['checks']['discovery_compute']['status'] = 'not_checked'
        self.reject('Ready pages')

    def test_successor_appends_discovery_and_cannot_rewrite_old_queries(self):
        from tests.test_page_successor import PageSuccessorTests
        self.partial_page()
        PageSuccessorTests.promote(self, 'hardware-status')
        self.add_query('discovery_data_path', 'GPU communication RDMA transfer in:readme', 'microsoft/mscclpp')
        self.notice()
        result = self.check()
        self.assertEqual(result['readiness'], 'partial')
        self.assertIn('discovery_data_path', result['coverage_checked'])
        self.audit['discovery']['queries'][0]['results'][0]['reason'] = 'Rewritten historical selection'
        self.reject('preserve all prior discovery')

    def fail_query(self, q, incomplete=False):
        source = next(s for s in self.audit['sources'] if s['id'] == q['source_id'])
        q['results'] = []
        if incomplete:
            q['truncated'] = True
            self.response(q, {'total_count': 5, 'incomplete_results': True, 'items': []})
        else:
            source.update(status='failed', error='Synthetic 403', content_sha256=None)
            source['attempt_records'][-1].update(status='failed', error=source['error'])
            q['response_sha256'] = None
        self.audit['checks'][q['family']].update(status='failed', failure_kind='incomplete_listing' if incomplete else 'access_denied')

    def selected_discovery_page(self):
        from tests.test_page_successor import PageSuccessorTests
        self.partial_page()
        q = self.audit['discovery']['queries'][2]
        result = q['results'][0]
        primary = result['repository_url'] + '/pull/123'
        self.audit['sources'][0]['url'] = self.candidate['source_url'] = primary
        self.draft['entries'][0]['source_urls'] = [primary]
        result.update(disposition='selected', novelty='new_event', event_key='cache-fix', source_ids=['source'])
        self.audit['checks'][q['family']]['status'] = 'checked'
        return result

    def test_published_discovery_selection_survives_later_page_rounds(self):
        from tests.test_page_successor import PageSuccessorTests
        result = self.selected_discovery_page()
        PageSuccessorTests.promote(self, 'hardware-status')
        self.add_query('discovery_data_path', 'GPU communication RDMA transfer in:readme', 'microsoft/mscclpp')
        self.notice()
        self.assertEqual(self.check()['readiness'], 'partial')
        PageSuccessorTests.promote(self, 'embodied-training')
        self.assertEqual(self.check()['readiness'], 'partial')
        self.assertNotIn('cache-fix', [c['event_key'] for c in self.candidates['items']])
        extra = self.add_query('discovery_compute', 'GPU driver throughput benchmark', 'JustAResearcher/gpu-unlock-ada', 'followup')
        extra['followup_of'] = result['url']
        extra['results'][0] = deepcopy(result)
        self.audit['discovery']['followup']['status'] = 'performed'
        self.reject('actual selection decision')
        self.audit['discovery']['queries'].pop()
        self.audit['discovery']['followup']['status'] = 'none_due'
        result['reason'] = 'Changed frozen historical selection'
        self.reject('preserve all prior discovery')

    def test_withdrawn_discovery_stays_historical_after_authenticated_correction(self):
        from tests.test_page_successor import PageSuccessorTests
        self.selected_discovery_page()
        PageSuccessorTests.promote(self, 'hardware-status')
        self.add_query('discovery_data_path', 'GPU communication RDMA transfer in:readme', 'microsoft/mscclpp')
        self.notice()
        PageSuccessorTests.correction_chain(self, withdraw=True)
        status = deepcopy(self.previous_report['entries'][0])
        status['event_key'] = 'withdrawn-discovery-status'
        path = self.root / 'previous/corrections.json'
        chain = json.loads(path.read_bytes())
        bundle = json.loads(base64.b64decode(chain[0]['bundle_base64']))
        file = bundle['files'][0]
        corrected = json.loads(base64.b64decode(file['base64']))
        corrected['entries'] = [status]
        raw = json.dumps(corrected).encode()
        file.update(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        raw = json.dumps(bundle).encode()
        chain[0]['bundle_base64'] = base64.b64encode(raw).decode()
        path.write_bytes(json.dumps(chain).encode())
        fingerprint = hashlib.sha256(raw).hexdigest()
        self.audit['page_scope']['previous_evidence']['bundle_sha256'] = fingerprint
        self.audit['page_scope']['page_delivery']['previous']['bundle_sha256'] = fingerprint
        self.draft['page_delivery']['previous']['bundle_sha256'] = fingerprint
        self.draft['entries'].insert(0, status)
        self.assertEqual(self.check()['readiness'], 'partial')
        self.assertNotIn('cache-fix', [e['event_key'] for e in self.draft['entries']])

    def retry_query(self, old):
        source = next(s for s in self.audit['sources'] if s['id'] == old['source_id'])
        if source['content_sha256'] is not None:
            self.root.joinpath('evidence', source['content_sha256'] + '.txt').write_bytes(
                self.root.joinpath('evidence', source['id'] + '.txt').read_bytes())
        source['attempt_records'].append(dict(source['attempt_records'][-1], status='ok', error=''))
        source.update(attempts=source['attempts'] + 1, elapsed_seconds=source['elapsed_seconds'] * 2,
                      status='ok', error='', retrieved_at='2026-11-06T07:24:00+08:00')
        q = deepcopy(old)
        q.update(id=old['id'] + '-retry', attempt=source['attempts'], truncated=False)
        self.audit['discovery']['queries'].append(q)
        self.response(q, {'total_count': 0, 'incomplete_results': False, 'items': []})
        check = self.audit['checks'][q['family']]
        check.update(status='no_qualifying_items')
        check.pop('failure_kind', None)
        return q

    def test_failed_query_recovers_in_successor_without_resetting_source_budget(self):
        from tests.test_page_successor import PageSuccessorTests
        self.partial_page()
        q = self.add_query('discovery_data_path', 'GPU communication RDMA transfer in:readme', 'microsoft/mscclpp')
        self.fail_query(q)
        self.notice()
        PageSuccessorTests.promote(self, 'hardware-status')
        self.retry_query(q)
        self.notice()
        result = self.check()
        self.assertIn('discovery_data_path', result['coverage_checked'])
        self.assertEqual(result['budget_accounting']['failed_attempts']['source'], 1)
        source = next(s for s in self.audit['sources'] if s['id'] == q['source_id'])
        source.update(elapsed_seconds=None, timing_unavailable_reason='Synthetic lost retry timer')
        source['attempt_records'][-1].update(elapsed_seconds=None, timing_unavailable_reason=source['timing_unavailable_reason'])
        unknown = self.check()
        self.assertTrue(unknown['budget_unknown'])
        self.assertEqual(unknown['readiness'], 'partial')
        self.assertGreater(unknown['budget_accounting']['charged_seconds'], result['budget_accounting']['charged_seconds'])

    def test_coverage_resolution_preserves_query_attempt_and_original_model_result(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                self.setUp()
                q = self.audit['discovery']['queries'][0]
                self.fail_query(q, incomplete)
                output = {key: deepcopy(self.audit[key]) for key in ('checks', 'sources', 'discovery')}
                output['candidates'] = deepcopy(self.candidates)
                output_sha = self.artifact(json.dumps(output).encode())
                call = self.audit['model_calls'][0]
                call.update(status='partial', partial_reason='coverage_incomplete', error='Original discovery gap',
                            coverage_resolution={'source_ids': [q['source_id']]})
                attempt = call['attempt_records'][0]
                attempt.update(status='partial', error=call['error'])
                original = dict(stage='research', stage_status='partial', partial_reason='coverage_incomplete', error=call['error'],
                                started_at='2026-11-06T07:22:40+08:00', finished_at='2026-11-06T07:23:00+08:00',
                                elapsed_seconds=attempt['elapsed_seconds'], input_sha256=attempt['input_sha256'], output_sha256=output_sha)
                attempt['result_sha256'] = self.artifact(json.dumps(original).encode())
                before = deepcopy(call)
                retry = self.retry_query(q)
                result = self.check()
                self.assertEqual(result['readiness'], 'ready')
                self.assertEqual(call, before)
                self.assertIn({'code': 'research_coverage_resolved', 'path': 'model_calls[0]'}, result['warnings'])
                self.assertEqual(retry['source_id'], q['source_id'])
                self.audit['limits']['source_attempts'] = 1
                self.assertEqual('source_budget_exceeded' in self.check()['blockers'], incomplete)
                retry['attempt'] = 1
                self.reject('distinct retained source attempt')

    def retained_detail_resolution(self):
        q = self.audit['discovery']['queries'][0]
        self.fail_query(q, True)
        output = {key: deepcopy(self.audit[key]) for key in ('checks', 'sources', 'discovery')}
        output['candidates'] = deepcopy(self.candidates)
        for check in output['checks'].values():
            check['detail'] = check.pop('reason')
        call = self.audit['model_calls'][0]
        call.update(status='partial', partial_reason='coverage_incomplete', error='Original discovery gap',
                    coverage_resolution={'source_ids': [q['source_id']]})
        call['attempt_records'][0].update(status='partial', error=call['error'])
        original = dict(stage='research', stage_status='partial', partial_reason='coverage_incomplete',
                        error=call['error'], started_at='2026-11-06T07:22:40+08:00',
                        finished_at='2026-11-06T07:23:00+08:00', elapsed_seconds=call['elapsed_seconds'],
                        input_sha256=call['attempt_records'][0]['input_sha256'])
        self.retry_query(q)
        return output, original, call

    def bind_retained_output(self, output, original, call):
        original['output_sha256'] = self.artifact(json.dumps(output).encode())
        call['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(original).encode())

    def test_resolved_discovery_accepts_retained_detail_without_changing_original(self):
        output, original, call = self.retained_detail_resolution()
        self.bind_retained_output(output, original, call)
        path = self.root / 'stage_artifacts' / (original['output_sha256'] + '.json')
        before, model = path.read_bytes(), deepcopy(call)
        self.assertEqual(self.check()['readiness'], 'ready')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(call, model)
        path.write_bytes(before + b' ')
        self.reject('Research output fingerprint')

    def test_retained_detail_does_not_relax_explanation_schema(self):
        for replacement, message in [
                ({'detail': 'one', 'reason': 'two'}, 'Ambiguous original coverage explanation'),
                ({'detail': 'same', 'reason': 'same'}, 'Ambiguous original coverage explanation'),
                ({}, 'Invalid original discovery schema'),
                ({'detail': None}, 'Invalid original discovery schema'),
                ({'detail': ''}, 'Invalid original discovery schema'),
                ({'detail': 'x' * 10001}, 'Invalid original discovery schema'),
                ({'detail': 'valid', 'extra': 'unknown'}, 'Invalid original discovery schema')]:
            with self.subTest(replacement=replacement):
                self.setUp()
                output, original, call = self.retained_detail_resolution()
                check = output['checks']['discovery_memory']
                check.pop('detail')
                check.update(replacement)
                self.bind_retained_output(output, original, call)
                self.reject(message)

    def test_retained_detail_keeps_candidate_source_and_query_guards(self):
        for changed in ('candidate', 'source', 'query'):
            with self.subTest(changed=changed):
                self.setUp()
                output, original, call = self.retained_detail_resolution()
                if changed == 'candidate':
                    output['candidates']['items'][0]['summary'] = 'Different candidate'
                    error = 'cannot introduce new research candidates'
                elif changed == 'source':
                    output['sources'][0]['url'] = 'https://example.com/changed'
                    error = 'retain all original source attempts'
                else:
                    output['discovery']['queries'][0]['query'] += ' changed'
                    error = 'query'
                self.bind_retained_output(output, original, call)
                self.reject(error)
