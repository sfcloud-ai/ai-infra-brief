"""离线合同回归；合成样本不代表真实研究或投递证据。"""
import hashlib
import unittest
from tests.test_required_coverage import RequiredCoverageTests


class LayeredBudgetTests(unittest.TestCase):
    save = RequiredCoverageTests.save
    check = RequiredCoverageTests.check
    reject = RequiredCoverageTests.reject
    failed = RequiredCoverageTests.failed

    def artifact(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.root.joinpath('stage_artifacts', digest + '.json').write_bytes(data)
        return digest

    def setUp(self):
        RequiredCoverageTests.setUp(self)
        self.root.joinpath('stage_artifacts').mkdir()
        self.audit['schema_version'] = '1.4'
        self.audit['budget_policy'] = 'successful_work_hourly_continuation_v1'
        self.audit['limits'] = dict(source_attempts=2, model_attempts=2,
                                   source_attempt_seconds=180, model_stage_seconds=500, run_seconds=1200)
        self.input = self.artifact(b'{"synthetic":"input"}')
        self.result = self.artifact(b'{"synthetic":"result"}')
        for source in self.audit['sources']:
            source['attempt_records'] = [dict(status='ok', elapsed_seconds=source['elapsed_seconds'], error='', reserved_seconds=180)]
        for call in self.audit['model_calls']:
            call['attempt_records'] = [dict(status='ok', elapsed_seconds=call['elapsed_seconds'], error='',
                input_sha256=self.input, checkpoint_sha256=None, result_sha256=self.result, reserved_seconds=400)]

    def test_source_attempts_separate_from_cumulative_wait_and_model_stage(self):
        source = self.audit['sources'][0]
        source.update(attempts=2, elapsed_seconds=220, attempt_records=[
            dict(status='timeout', elapsed_seconds=100, error='synthetic timeout', reserved_seconds=180),
            dict(status='ok', elapsed_seconds=100, error='', reserved_seconds=180)])
        call = self.audit['model_calls'][0]
        call['elapsed_seconds'] = call['attempt_records'][0]['elapsed_seconds'] = 300
        self.assertEqual(self.check()['readiness'], 'ready')
        source['attempt_records'][0]['elapsed_seconds'] = 181
        source['elapsed_seconds'] = 300
        self.assertEqual(self.check()['readiness'], 'ready')
        source['attempt_records'][1]['elapsed_seconds'] = 181
        source['elapsed_seconds'] = 400
        self.assertIn('source_budget_exceeded', self.check()['blockers'])

    def test_transport_gap_does_not_fail_other_research(self):
        source = self.failed(kind='transport')
        source['attempt_records'] = [dict(status='failed', elapsed_seconds=1, error=source['error'], reserved_seconds=180)]
        self.assertEqual(self.check()['readiness'], 'partial')

    def test_known_source_then_failed_render_retains_success_and_unknown_reservation(self):
        baseline = self.check()['budget_accounting']
        source = self.failed(kind='transport')
        source.update(attempts=2, elapsed_seconds=None,
                      timing_unavailable_reason='synthetic browser timing loss')
        source['attempt_records'] = [
            dict(status='ok', elapsed_seconds=3.932651239, error='', reserved_seconds=60),
            dict(status='failed', elapsed_seconds=None, error=source['error'], reserved_seconds=60,
                 timing_unavailable_reason=source['timing_unavailable_reason'])]
        result = self.check()
        self.assertEqual(result['readiness'], 'partial')
        self.assertEqual(result['coverage_incomplete'], ['qwen_blog'])
        self.assertTrue(result['budget_unknown'])
        accounting = result['budget_accounting']
        self.assertAlmostEqual(accounting['successful_seconds'], baseline['successful_seconds'] - 1 + 3.932651239)
        self.assertEqual(accounting['successful_attempts'], baseline['successful_attempts'])
        self.assertEqual(accounting['unknown_attempts']['source'], 1)
        self.assertEqual(accounting['reserved_unknown_seconds'], 60)
        self.assertAlmostEqual(accounting['charged_seconds'], accounting['successful_seconds'] + 60)
        self.assertEqual(source['attempt_records'][0]['status'], 'ok')

    def test_known_source_reread_charges_both_successes_and_enforces_quota(self):
        baseline = self.check()['budget_accounting']
        source = self.audit['sources'][0]
        previous = dict(source['attempt_records'][0])
        source.update(attempts=2, elapsed_seconds=previous['elapsed_seconds'] * 2,
                      attempt_records=[previous, dict(previous)])
        accounting = self.check()['budget_accounting']
        self.assertEqual(accounting['successful_seconds'], baseline['successful_seconds'] + previous['elapsed_seconds'])
        self.assertEqual(accounting['successful_attempts']['source'], baseline['successful_attempts']['source'] + 1)
        self.audit['limits']['source_attempts'] = 1
        self.assertIn('source_budget_exceeded', self.check()['blockers'])
        self.audit['limits']['source_attempts'] = 2
        source['attempt_records'].append(dict(previous))
        source.update(attempts=3, elapsed_seconds=previous['elapsed_seconds'] * 3)
        self.assertIn('source_budget_exceeded', self.check()['blockers'])

    def test_source_reread_still_rejects_unknown_previous_outcome_or_timing(self):
        source = self.audit['sources'][0]
        previous = dict(source['attempt_records'][0])
        source.update(attempts=2, elapsed_seconds=previous['elapsed_seconds'] * 2,
                      attempt_records=[dict(previous, status='unknown', error='unconfirmed'), dict(previous)])
        self.reject('Cannot retry')
        source['attempt_records'][0] = dict(previous, elapsed_seconds=None,
                                            timing_unavailable_reason='synthetic missing timing')
        source.update(elapsed_seconds=None, timing_unavailable_reason='synthetic missing timing')
        self.reject('Unknown attempt cannot be replayed')

    def test_source_reread_does_not_allow_completed_model_retry(self):
        call = self.recover()
        call['attempt_records'][0].update(status='ok', error='')
        self.reject('Cannot retry')

    def recover(self):
        call = self.audit['model_calls'][0]
        old = dict(call['attempt_records'][0], status='timeout', error='synthetic interruption', elapsed_seconds=200)
        call.update(attempts=2, elapsed_seconds=400, attempt_records=[old, dict(old, status='ok', error='',
            checkpoint_sha256=self.result, elapsed_seconds=200)])
        return call

    def test_recovered_model_keeps_original_failure_and_checkpoint(self):
        call = self.recover()
        self.assertEqual(self.check()['readiness'], 'ready')
        call['elapsed_seconds'] = 501
        self.reject('must equal retained attempt durations')
        call['elapsed_seconds'] = 400
        call['attempt_records'][1]['checkpoint_sha256'] = self.input
        self.reject('preceding result')

    def test_known_attempt_sum_cannot_hide_in_unknown_aggregate(self):
        call = self.recover()
        call.update(elapsed_seconds=None, timing_unavailable_reason='synthetic loss')
        call['attempt_records'][0]['elapsed_seconds'] = 300
        call['attempt_records'][1]['elapsed_seconds'] = 300
        self.assertEqual(self.check()['readiness'], 'ready')
        call['attempt_records'][1]['elapsed_seconds'] = 501
        self.reject('sequential attempt durations')

    def test_unknown_model_result_cannot_be_retried(self):
        call = self.recover()
        call['attempt_records'][0].update(elapsed_seconds=None, timing_unavailable_reason='synthetic loss')
        self.reject('cannot be replayed')

    def test_actual_final_failure_and_missing_editor_still_block(self):
        call = self.audit['model_calls'][0]
        call.update(status='failed', error='synthetic final failure')
        call['attempt_records'][0].update(status='failed', error=call['error'])
        self.assertIn('model_stage_failed', self.check()['blockers'])
        self.audit['model_calls'].pop()
        self.assertIn('model_stage_missing', self.check()['blockers'])

    def test_required_records_and_artifact_bytes_and_counts(self):
        call = self.audit['model_calls'][0]
        call['attempts'] = 2
        self.reject('Attempt count')
        call['attempts'] = 1
        self.root.joinpath('stage_artifacts', self.input + '.json').write_bytes(b'changed')
        self.reject('fingerprint')

    def test_unknown_attempt_time_warns_without_fabricating_zero(self):
        source = self.audit['sources'][0]
        source.update(elapsed_seconds=None, timing_unavailable_reason='synthetic loss')
        source['attempt_records'][0].update(elapsed_seconds=None, timing_unavailable_reason='synthetic loss')
        result = self.check()
        self.assertTrue(result['budget_unknown'])
        self.assertEqual(result['readiness'], 'ready')

    def test_legacy_contract_not_reinterpreted(self):
        self.audit['schema_version'] = '1.3'
        self.reject('metadata schema')
        self.audit['limits'] = dict(source_attempts=2,model_attempts=1,operation_seconds=180,run_seconds=1200)
        self.reject('metadata schema')

    def test_many_failures_across_hours_do_not_consume_success_quota(self):
        call = self.recover()
        failed = call['attempt_records'][0]
        call['attempt_records'] = [failed] + [dict(failed, checkpoint_sha256=self.result)] * 3 + [call['attempt_records'][1]]
        call.update(attempts=5, elapsed_seconds=1000)
        self.audit['finished_at'] = '2026-11-06T12:30:00+08:00'
        result = self.check()
        self.assertEqual(result['readiness'], 'ready')
        self.assertEqual(result['budget_accounting']['failed_attempts']['model'], 4)
        self.assertEqual(result['budget_accounting']['failed_seconds'], 800)
        self.assertEqual(result['budget_accounting']['successful_seconds'], 249)
        self.assertEqual(result['budget_accounting']['charged_seconds'], 249)
        self.assertEqual(result['run_elapsed_seconds'], 18600)
        self.audit['limits']['run_seconds'] = 248
        self.assertIn('run_budget_exceeded', self.check()['blockers'])

    def test_unknown_outcome_cannot_release_or_finish(self):
        call = self.audit['model_calls'][0]
        call.update(status='unknown', error='unconfirmed outcome')
        call['attempt_records'][0].update(status='unknown', error=call['error'])
        result = self.check()
        self.assertIn('operation_outcome_unknown', result['blockers'])
        self.assertTrue(result['budget_unknown'])
        self.assertEqual(result['budget_accounting']['reserved_unknown_seconds'], 400)
        self.assertEqual(result['budget_accounting']['unknown_attempts']['model'], 1)
        self.assertEqual(result['budget_accounting']['failed_attempts']['model'], 0)

    def test_unknown_uses_exact_original_reservation_not_stage_cap(self):
        call = self.audit['model_calls'][0]
        call.update(elapsed_seconds=None, timing_unavailable_reason='synthetic missing duration')
        call['attempt_records'][0].update(elapsed_seconds=None, timing_unavailable_reason='synthetic missing duration', reserved_seconds=70)
        result = self.check()
        self.assertEqual(result['budget_accounting']['reserved_unknown_seconds'], 70)
        call['attempt_records'][0]['reserved_seconds'] = 501
        self.reject('reservation exceeds')
        del call['attempt_records'][0]['reserved_seconds']
        self.reject('metadata schema')

    def test_repeated_large_artifact_counts_once(self):
        artifact = self.artifact(b'{"synthetic":"' + b'x' * 1_800_000 + b'"}')
        call = self.recover()
        failed = dict(call['attempt_records'][0], input_sha256=artifact, result_sha256=artifact)
        call['attempt_records'] = [failed] + [dict(failed, checkpoint_sha256=artifact)] * 3 + [
            dict(call['attempt_records'][1], input_sha256=artifact, checkpoint_sha256=artifact, result_sha256=artifact)]
        call.update(attempts=5, elapsed_seconds=1000)
        self.audit['finished_at'] = '2026-11-06T12:30:00+08:00'
        self.assertEqual(self.check()['readiness'], 'ready')

    def test_recovery_records_new_evidence_input_without_overwriting_previous(self):
        call = self.recover()
        new_input = self.artifact(b'{"synthetic":"additional source bodies"}')
        call['attempt_records'][1]['input_sha256'] = new_input
        self.assertEqual(self.check()['readiness'], 'ready')
        self.assertNotEqual(call['attempt_records'][0]['input_sha256'], new_input)
        self.root.joinpath('stage_artifacts', self.input + '.json').unlink()
        self.reject('Missing audit file')

    def test_unknown_known_duration_reserves_observed_cost_if_larger(self):
        call = self.audit['model_calls'][0]
        call.update(status='unknown', error='unconfirmed', elapsed_seconds=450)
        call['attempt_records'][0].update(status='unknown', error='unconfirmed', elapsed_seconds=450, reserved_seconds=400)
        result = self.check()
        self.assertEqual(result['budget_accounting']['reserved_unknown_seconds'], 450)
        self.assertIn('operation_outcome_unknown', result['blockers'])

    def test_successful_model_requires_saved_result_bytes(self):
        call = self.audit['model_calls'][0]
        call['attempt_records'][0]['result_sha256'] = None
        self.reject('requires its saved result')

    def test_known_aggregate_cannot_be_hidden_by_smaller_unknown_reservation(self):
        call = self.audit['model_calls'][0]
        call.update(elapsed_seconds=550)
        call['attempt_records'][0].update(elapsed_seconds=None, timing_unavailable_reason='synthetic missing breakdown', reserved_seconds=400)
        self.reject('Known aggregate duration requires complete per-attempt timing')

    def test_model_aggregate_cannot_hide_unexplained_time(self):
        call = self.audit['model_calls'][0]
        call['elapsed_seconds'] = 550
        self.reject('must equal retained attempt durations')
        self.audit['limits']['run_seconds'] = 100
        self.reject('must equal retained attempt durations')

    def test_model_aggregate_allows_only_microsecond_rounding(self):
        call = self.audit['model_calls'][0]
        original = call['elapsed_seconds']
        call['elapsed_seconds'] = original + 0.0000005
        self.assertEqual(self.check()['readiness'], 'ready')
        call['elapsed_seconds'] = original + 0.000002
        self.reject('must equal retained attempt durations')

    def test_full_model_artifacts_use_total_package_not_metadata_size_limit(self):
        large = self.artifact(b'{"synthetic":"' + b'x' * 2_800_000 + b'"}')
        self.audit['model_calls'][0]['attempt_records'][0]['input_sha256'] = large
        self.assertEqual(self.check()['readiness'], 'ready')

    def test_layered_success_quota_preserves_raw_float_boundary(self):
        self.audit['limits'].update(model_stage_seconds=3600, run_seconds=3600)
        self.audit['finished_at'] = '2026-11-06T08:20:00+08:00'
        durations = [36.7] * 19 + [0.3, 100.1, 2802.3]
        records = self.audit['sources'] + self.audit['model_calls']
        self.assertEqual(len(records), len(durations))
        for record, duration in zip(records, durations):
            record['elapsed_seconds'] = record['attempt_records'][0]['elapsed_seconds'] = duration
        original = self.audit['limits'].copy()
        result = self.check()
        self.assertEqual(result['readiness'], 'ready')
        raw_total = 0
        for duration in durations:
            raw_total += duration
        self.assertEqual(result['budget_accounting']['charged_seconds'], raw_total)
        self.assertGreater(result['budget_accounting']['charged_seconds'], 3600)
        self.assertEqual(self.audit['limits'], original)
        records[-1]['elapsed_seconds'] += 0.000002
        records[-1]['attempt_records'][0]['elapsed_seconds'] += 0.000002
        self.assertIn('run_budget_exceeded', self.check()['blockers'])

    def test_layered_success_operation_cap_has_bounded_microsecond_tolerance(self):
        for kind, record, cap in [('source', self.audit['sources'][0], 180),
                                  ('model', self.audit['model_calls'][0], 500)]:
            with self.subTest(kind=kind):
                old = record['elapsed_seconds']
                record['elapsed_seconds'] = record['attempt_records'][0]['elapsed_seconds'] = cap + 0.0000005
                self.assertNotIn(kind + '_budget_exceeded', self.check()['blockers'])
                record['elapsed_seconds'] = record['attempt_records'][0]['elapsed_seconds'] = cap + 0.000002
                self.assertIn(kind + '_budget_exceeded', self.check()['blockers'])
                record['elapsed_seconds'] = record['attempt_records'][0]['elapsed_seconds'] = old

    def test_layered_duration_run_interval_tolerance_is_bounded(self):
        call = self.audit['model_calls'][0]
        self.audit['limits']['model_stage_seconds'] = 1200
        call['elapsed_seconds'] = call['attempt_records'][0]['elapsed_seconds'] = 600 + 0.0000005
        self.assertEqual(self.check()['readiness'], 'ready')
        call['elapsed_seconds'] = call['attempt_records'][0]['elapsed_seconds'] = 600 + 0.000002
        self.reject('duration exceeds recorded run interval')

    def test_layered_sequential_attempt_sum_tolerance_is_bounded(self):
        source = self.failed(kind='transport')
        source.update(attempts=2, elapsed_seconds=None, timing_unavailable_reason='synthetic aggregate loss')
        source['attempt_records'] = [dict(status='timeout', elapsed_seconds=300.0000005, error='synthetic timeout', reserved_seconds=180),
                                     dict(status='failed', elapsed_seconds=300, error='synthetic failure', reserved_seconds=180)]
        source.update(status='failed', error='synthetic failure')
        # 此夹具仍有原来源证据；这里只审计累计时长边界，不构成来源访问证明。
        self.assertNotIn('source_budget_exceeded', self.check()['blockers'])
        source['attempt_records'][0]['elapsed_seconds'] = 300.000002
        self.reject('Known sequential attempt durations exceed')
