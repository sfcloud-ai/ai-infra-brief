"""合成覆盖补证回归；不作为真实研究或送达证明。"""
from copy import deepcopy
import json
import hashlib
import unittest

from tests import test_layered_budget as layered


class ResolvedCoverageTests(unittest.TestCase):
    save = layered.LayeredBudgetTests.save
    check = layered.LayeredBudgetTests.check
    reject = layered.LayeredBudgetTests.reject
    artifact = layered.LayeredBudgetTests.artifact

    def setUp(self):
        layered.LayeredBudgetTests.setUp(self)
        self.original_sources = deepcopy(self.audit['sources'])
        prior_source = next(s for s in self.original_sources if s['id'] == 'coverage-qwen_blog')
        prior_source['retrieved_at'] = '2026-11-06T07:20:30+08:00'
        current_source = next(s for s in self.audit['sources'] if s['id'] == 'coverage-qwen_blog')
        current_source['attempt_records'].append(deepcopy(current_source['attempt_records'][0]))
        current_source['attempts'] = 2
        current_source['elapsed_seconds'] *= 2
        current_source['retrieved_at'] = '2026-11-06T07:24:00+08:00'
        original_checks = deepcopy(self.audit['checks'])
        original_checks['qwen_blog'].update(status='failed', failure_kind='incomplete_listing')
        output = dict(candidates=deepcopy(self.candidates), checks=original_checks,
                      sources=self.original_sources)
        self.output_sha = self.artifact(json.dumps(output).encode())
        call = self.audit['model_calls'][0]
        call.update(status='partial', partial_reason='coverage_incomplete', error='Original coverage gap',
                    coverage_resolution=dict(source_ids=['coverage-qwen_blog']))
        attempt = call['attempt_records'][0]
        attempt.update(status='partial', error=call['error'])
        self.original = dict(stage='research', stage_status='partial', partial_reason='coverage_incomplete',
                             error=call['error'], started_at='2026-11-06T07:22:40+08:00',
                             elapsed_seconds=attempt['elapsed_seconds'], input_sha256=attempt['input_sha256'],
                             output_sha256=self.output_sha, finished_at='2026-11-06T07:23:00+08:00')
        attempt['result_sha256'] = self.artifact(json.dumps(self.original).encode())

    def test_resolved_gap_preserves_stage_and_budget_without_replay(self):
        before = deepcopy(self.audit['model_calls'])
        result = self.check()
        self.assertEqual(result['readiness'], 'ready')
        self.assertEqual(result['blockers'], [])
        self.assertIn({'code': 'research_coverage_resolved', 'path': 'model_calls[0]'}, result['warnings'])
        self.assertEqual(self.audit['model_calls'], before)
        self.assertEqual(result['budget_accounting']['successful_attempts']['model'], 2)

    def test_legacy_or_unbound_partial_remains_rejected(self):
        self.audit['model_calls'][0].pop('coverage_resolution')
        self.reject('Partial research requires')

    def test_resolution_cannot_hide_a_reused_source_attempt(self):
        source = next(s for s in self.audit['sources'] if s['id'] == 'coverage-qwen_blog')
        source['attempt_records'].pop()
        source['attempts'] = 1
        source['elapsed_seconds'] /= 2
        self.reject('appended budgeted attempt')

    def test_added_source_attempt_still_enforces_success_budget(self):
        self.audit['limits']['source_attempts'] = 1
        self.assertIn('source_budget_exceeded', self.check()['blockers'])

    def test_original_gap_requires_consistent_source_evidence(self):
        path = self.root / 'stage_artifacts' / (self.output_sha + '.json')
        original = json.loads(path.read_bytes())
        for check, error in [({'status': 'failed'}, 'source references'),
                             (dict(original['checks']['qwen_blog'], failure_kind='access_denied'), 'failed-source evidence')]:
            changed = deepcopy(original)
            changed['checks']['qwen_blog'] = check
            self.original['output_sha256'] = self.artifact(json.dumps(changed).encode())
            self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
            self.reject(error)

    def test_non_candidate_notes_can_be_normalized_without_new_research(self):
        self.candidates['notes'] = ['仅补齐格式字段，不改变候选事实']
        self.assertEqual(self.check()['readiness'], 'ready')

    def test_resolution_requires_original_result_and_output_bytes(self):
        self.root.joinpath('stage_artifacts', self.output_sha + '.json').write_bytes(b'{}')
        self.reject('Research output fingerprint')

    def test_resolution_cannot_add_candidates_or_drop_attempts(self):
        self.candidates['items'][0]['summary'] = 'Changed research fact'
        self.reject('cannot introduce new research candidates')
        self.candidates['items'][0]['summary'] = '合成测试事实'
        self.audit['sources'][0]['elapsed_seconds'] += 1
        self.audit['sources'][0]['attempt_records'][0]['elapsed_seconds'] += 1
        self.reject('retain all original source attempts')

    def test_evidence_must_be_later_and_cover_the_original_gap(self):
        call = self.audit['model_calls'][0]
        self.original['finished_at'] = '2026-11-06T07:25:00+08:00'
        self.original['started_at'] = '2026-11-06T07:24:40+08:00'
        call['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.reject('retrieved after')
        self.original['finished_at'] = '2026-11-06T07:23:00+08:00'
        self.original['started_at'] = '2026-11-06T07:22:40+08:00'
        call['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        other = next(s for s in self.audit['sources'] if s['id'] == 'coverage-glm_blog')
        other['attempt_records'].append(deepcopy(other['attempt_records'][0]))
        other['attempts'] = 2
        other['elapsed_seconds'] *= 2
        other['retrieved_at'] = '2026-11-06T07:24:00+08:00'
        call['coverage_resolution']['source_ids'] = ['coverage-glm_blog']
        self.reject('every original gap')

    def test_resolution_cannot_relabel_failure_or_ignore_a_current_gap(self):
        self.original['stage_status'] = 'failed'
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.reject('preserve the original partial result')
        self.original['stage_status'] = 'partial'
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.audit['checks']['qwen_blog']['status'] = 'not_checked'
        self.audit['coverage_notice'] = None
        self.reject('completed current checks')

    def test_changed_response_requires_original_bytes(self):
        source = next(s for s in self.audit['sources'] if s['id'] == 'coverage-qwen_blog')
        path = self.root / 'evidence' / (source['id'] + '.txt')
        original_bytes, original_hash = path.read_bytes(), source['content_sha256']
        path.write_bytes(b'Synthetic completed listing')
        source['content_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.reject('Missing audit file')
        retained = self.root / 'evidence' / (original_hash + '.txt')
        retained.write_bytes(original_bytes)
        self.assertEqual(self.check()['readiness'], 'ready')
        retained.write_bytes(b'changed')
        self.reject('Original source response fingerprint')

    def test_original_result_time_and_duration_are_bound(self):
        for start, end in [('2020-11-06T07:22:40+08:00', '2020-11-06T07:23:00+08:00'),
                           ('2026-11-06T07:31:40+08:00', '2026-11-06T07:32:00+08:00'),
                           ('2026-11-06T07:20:00+08:00', '2026-11-06T07:23:00+08:00')]:
            self.original.update(started_at=start, finished_at=end)
            self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
            self.reject('audited run and duration')

    def test_malformed_retained_attempt_is_a_validation_error(self):
        original = json.loads((self.root / 'stage_artifacts' / (self.output_sha + '.json')).read_bytes())
        for record in [None, {}, {'status': 'ok'}]:
            changed = deepcopy(original)
            changed['sources'][0]['attempt_records'] = [record]
            self.original['output_sha256'] = self.artifact(json.dumps(changed).encode())
            self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
            self.reject('retained research source or coverage schema')

    def test_changed_unlisted_source_requires_a_charged_attempt(self):
        self.audit['limits']['run_seconds'] = self.check()['budget_accounting']['charged_seconds']
        source = next(s for s in self.audit['sources'] if s['id'] == 'coverage-glm_blog')
        source['retrieved_at'] = '2026-11-06T07:25:00+08:00'
        self.reject('appended budgeted attempt')
        source['attempt_records'].append(deepcopy(source['attempt_records'][-1]))
        source['attempts'] += 1
        source['elapsed_seconds'] *= 2
        self.assertIn('run_budget_exceeded', self.check()['blockers'])

    def test_same_id_reread_preserves_original_candidate_evidence(self):
        source = self.audit['sources'][0]
        original_time = self.candidates['items'][0]['retrieved_at']
        source['retrieved_at'] = '2026-11-06T07:25:00+08:00'
        source['attempt_records'].append(deepcopy(source['attempt_records'][-1]))
        source['attempts'] += 1
        source['elapsed_seconds'] *= 2
        self.assertEqual(self.check()['readiness'], 'ready')
        path = self.root / 'evidence' / (source['id'] + '.txt')
        (self.root / 'evidence' / (source['content_sha256'] + '.txt')).write_bytes(path.read_bytes())
        path.write_bytes(b'New response without the old candidate evidence')
        source['content_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.check()['readiness'], 'ready')
        self.assertEqual(self.candidates['items'][0]['retrieved_at'], original_time)

    def test_failed_reread_keeps_verified_candidate_and_failure_accounting(self):
        baseline = self.check()['budget_accounting']
        source = self.audit['sources'][0]
        path = self.root / 'evidence' / (source['id'] + '.txt')
        retained = self.root / 'evidence' / (source['content_sha256'] + '.txt')
        retained.write_bytes(path.read_bytes())
        source['attempt_records'].append(dict(source['attempt_records'][-1], status='failed', error='Synthetic transport failure'))
        source.update(attempts=2, elapsed_seconds=source['elapsed_seconds'] * 2, status='failed',
                      error='Synthetic transport failure', content_sha256=None,
                      retrieved_at='2026-11-06T07:25:00+08:00')
        result = self.check()
        self.assertEqual(result['readiness'], 'ready')
        self.assertEqual(result['selected_count'], 1)
        self.assertEqual(result['budget_accounting']['successful_seconds'], baseline['successful_seconds'])
        self.assertEqual(result['budget_accounting']['failed_attempts']['source'], 1)
        retained.unlink()
        self.reject('Missing audit file')

    def test_later_read_cannot_retroactively_verify_original_candidate(self):
        output = json.loads((self.root / 'stage_artifacts' / (self.output_sha + '.json')).read_bytes())
        self.candidates['items'][0]['retrieved_at'] = '2026-11-06T07:25:00+08:00'
        output['candidates'] = deepcopy(self.candidates)
        self.original['output_sha256'] = self.artifact(json.dumps(output).encode())
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        source = self.audit['sources'][0]
        source['attempt_records'].append(deepcopy(source['attempt_records'][-1]))
        source.update(attempts=2, elapsed_seconds=source['elapsed_seconds'] * 2,
                      retrieved_at=self.candidates['items'][0]['retrieved_at'])
        self.reject('Candidate needs matching publication, retrieval and source evidence')

    def test_appended_attempt_cannot_reduce_recorded_cumulative_time(self):
        output = json.loads((self.root / 'stage_artifacts' / (self.output_sha + '.json')).read_bytes())
        prior = next(s for s in output['sources'] if s['id'] == 'coverage-qwen_blog')
        prior['elapsed_seconds'] = 50
        self.original['output_sha256'] = self.artifact(json.dumps(output).encode())
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.reject('preserve recorded cumulative source time')
        source = next(s for s in self.audit['sources'] if s['id'] == prior['id'])
        source['elapsed_seconds'] = 51
        self.assertEqual(self.check()['readiness'], 'ready')

    def test_resolution_preserves_original_model_error(self):
        self.audit['model_calls'][0]['error'] = 'Different original error'
        self.reject('preserve the original partial result')

    def test_malformed_retained_check_status_is_a_validation_error(self):
        output = json.loads((self.root / 'stage_artifacts' / (self.output_sha + '.json')).read_bytes())
        for status in [[], {}]:
            output['checks']['qwen_blog']['status'] = status
            self.original['output_sha256'] = self.artifact(json.dumps(output).encode())
            self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
            self.reject('original completed research coverage record')

    def test_original_coverage_explanations_need_not_use_audit_field_names(self):
        output = json.loads((self.root / 'stage_artifacts' / (self.output_sha + '.json')).read_bytes())
        for check in output['checks'].values():
            check['detail'] = check.pop('reason')
        self.original['output_sha256'] = self.artifact(json.dumps(output).encode())
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.assertEqual(self.check()['readiness'], 'ready')

    def test_missing_elapsed_remains_a_budget_warning_with_bound_timestamps(self):
        call = self.audit['model_calls'][0]
        call.update(elapsed_seconds=None, timing_unavailable_reason='合成计时缺失')
        call['attempt_records'][0].update(elapsed_seconds=None, timing_unavailable_reason='合成计时缺失')
        self.original['elapsed_seconds'] = None
        call['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        result = self.check()
        self.assertTrue(result['budget_unknown'])
        self.assertEqual(result['readiness'], 'ready')

    def test_original_finish_cannot_precede_its_source_reads(self):
        self.original.update(started_at='2026-11-06T07:20:40+08:00', finished_at='2026-11-06T07:21:00+08:00')
        self.audit['model_calls'][0]['attempt_records'][0]['result_sha256'] = self.artifact(json.dumps(self.original).encode())
        self.reject('finish before its retained sources')


if __name__ == '__main__':
    unittest.main()
