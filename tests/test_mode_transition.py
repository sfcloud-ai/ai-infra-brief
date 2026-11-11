"""仅构造容器、配置与账本，覆盖迁移故障，不连接 Docker 或网络。"""
import copy
import hashlib
import json
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy import mode_transition as mode
from deploy.policy import Policy
from deploy.release import write_json
from deploy.supervisor import ServiceError, CutoverDeferred
from deploy.ssh_entry import parse_command

SHA = 'a' * 40
CHECKSUM = 'b' * 64


def binding(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'scheduler.json'
        self.original = {'activation_date': '2026-11-04', 'publisher_uid': 996, 'scope': 'synthetic'}
        write_json(self.config_path, self.original)
        self.old_binding = binding(self.original)
        self.command = ('scheduled-only', '10', SHA, self.old_binding)
        self.policy = Policy(self.root)
        self.policy.accept(10, SHA, CHECKSUM)
        self.policy.completed(SHA)
        ready = self.root / 'releases' / SHA / 'ready.json'
        ready.parent.mkdir(parents=True)
        write_json(ready, {'commit': SHA, 'bundle_sha256': CHECKSUM})
        self.activation = self.root / 'activation'
        self.activation.mkdir()
        for name in ('active.json', 'permit.json'):
            write_json(self.activation / name, {'release': SHA})
        self.container = self.item('old', self.old_binding, True)
        self.phase = 'active'
        self.fail = None
        self.trace = []
        self.ledger = {'requests': ['auto/sent', 'scheduled/sent'], 'cursor': 'unchanged'}
        self.initial_ledger = copy.deepcopy(self.ledger)
        self.pending = False
        for obj, name, value in [(mode.scheduler_service, 'CONFIG', self.config_path),
                                  (mode.scheduler_service, 'admin_path', lambda *_: None)]:
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def item(self, identity, bind, running):
        return {'Id': identity, 'State': {'Running': running},
                'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}},
                'Config': {'Labels': {mode.scheduler_service.LABEL: bind}}}

    def event(self, value):
        self.trace.append(value)
        if self.fail == value:
            self.fail = None
            raise RuntimeError('synthetic interruption ' + value)

    def factory(self, *_):
        value = json.loads(self.config_path.read_text())
        scheduler = SimpleNamespace(value=value, binding=binding(value), binding_for=binding,
                                    preflight=lambda *a, **kw: None)
        driver = SimpleNamespace(scheduler=scheduler, activation=self.activation,
                                 preflight=lambda _: None, preflight_rollback=lambda _: None,
                                 code=lambda _: '/code', runtime=lambda _: '/runtime')
        def assert_binding():
            if self.container is None or self.container['Config']['Labels'][mode.scheduler_service.LABEL] != scheduler.binding:
                raise ServiceError('binding_changed')
        def quiesce(_):
            if self.pending:
                raise CutoverDeferred('pending or unknown')
            self.phase = 'paused'
            for name in ('active.json', 'permit.json'):
                write_json(self.activation / name, {'release': None})
            self.container['HostConfig']['RestartPolicy']['Name'] = 'no'
            self.container['State']['Running'] = False
            self.event('stopped')
        def check(*a, **kw):
            self.event('offline_check')
            if self.pending:
                raise ServiceError('pending or unknown')
        def start(_):
            if self.container is not None and self.container['State']['Running']:
                raise ServiceError('parallel_sender')
            self.container = self.item('new', scheduler.binding, True)
            self.phase = 'paused'
            self.event('started')
        def ready(_):
            if self.pending or self.phase != 'paused':
                raise ServiceError('not_ready')
            self.event('ready')
        def activate(_):
            self.phase = 'active'
            self.event('activated')
        def healthy(_):
            if self.phase != 'active':
                raise ServiceError('not_active')
            self.event('healthy')
        driver.assert_scheduler_binding = assert_binding
        driver.quiesce, driver.one_shot, driver.start_paused = quiesce, check, start
        driver.status = lambda _: {'phase': self.phase}
        driver.wait_ready, driver.activate, driver.wait_healthy = ready, activate, healthy
        return driver

    def inventory(self, driver, release, allowed):
        if self.container and self.container['Config']['Labels'][mode.scheduler_service.LABEL] not in allowed:
            raise ServiceError('identity_changed')
        return copy.deepcopy(self.container)

    def invoke(self):
        return mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                            self.factory, source_check=lambda *args, **kwargs: None, inspect=self.inventory)

    def test_success_preserves_scope_and_ledger_and_repeated_command(self):
        self.assertEqual(self.invoke()['status'], 'scheduled_only')
        self.invoke()
        self.assertEqual(self.trace.count('activated'), 1)
        self.assertEqual(self.trace.count('started'), 1)
        self.assertEqual(self.ledger, self.initial_ledger)
        self.assertEqual(json.loads(self.config_path.read_text()),
                         {**self.original, 'automatic_delivery': 'scheduled_only'})
        mode.guard(self.root)

    def test_recover_each_interruption_without_parallel_sender_or_reactivation(self):
        for failure in ('stopped', 'offline_check', 'started', 'ready', 'activated', 'healthy'):
            with self.subTest(failure=failure):
                self.setUp()
                self.fail = failure
                with self.assertRaises(RuntimeError):
                    self.invoke()
                with self.assertRaisesRegex(ServiceError, 'forward_recovery'):
                    mode.guard(self.root)
                self.invoke()
                self.assertEqual(self.trace.count('activated'), 1)
                self.assertEqual(self.trace.count('started'), 1)
                self.assertEqual(self.ledger, self.initial_ledger)

    def test_pending_unknown_block_drain_without_rewriting_state(self):
        self.pending = True
        with self.assertRaises(CutoverDeferred):
            self.invoke()
        self.assertEqual(self.ledger, self.initial_ledger)
        self.assertEqual(json.loads(self.config_path.read_text()), self.original)
        self.assertTrue(self.container['State']['Running'])
        self.pending = False
        self.invoke()

    def test_unknown_after_stop_blocks_configuration_change(self):
        self.fail = 'stopped'
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.pending = True
        with self.assertRaisesRegex(ServiceError, 'unknown'):
            self.invoke()
        self.assertEqual(json.loads(self.config_path.read_text()), self.original)
        self.assertNotIn('started', self.trace)

    def test_binding_change_during_interruption_blocks(self):
        self.fail = 'stopped'
        with self.assertRaises(RuntimeError):
            self.invoke()
        write_json(self.config_path, {**self.original, 'activation_date': '2026-11-05'})
        with self.assertRaisesRegex(ServiceError, 'scope_changed'):
            self.invoke()
        self.assertNotIn('started', self.trace)

    def test_mode_regression_after_completion_is_rejected(self):
        self.invoke()
        write_json(self.config_path, self.original)
        with self.assertRaisesRegex(ServiceError, 'mode_regression'):
            self.invoke()
        with self.assertRaisesRegex(ServiceError, 'mode_regression'):
            mode.guard(self.root)

    def test_active_sha_and_ready_checksum_are_checked_before_stop(self):
        self.policy.value['active'] = 'f' * 40
        with self.assertRaisesRegex(ServiceError, 'active_identity'):
            self.invoke()
        self.policy.value['active'] = SHA
        self.policy.value['accepted']['checksum'] = 'c' * 64
        with self.assertRaisesRegex(ServiceError, 'release_identity'):
            self.invoke()
        self.assertEqual(self.trace, [])

    def test_activation_intent_missing_container_never_restarts_unknown_work(self):
        self.fail = 'activated'
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.container = None
        with self.assertRaisesRegex(ServiceError, 'outcome_requires_reconciliation'):
            self.invoke()
        self.assertEqual(self.trace.count('started'), 1)

    def test_crash_after_config_replace_recovers_forward(self):
        atomic = mode.control_entry.atomic
        def interrupted(*args, **kwargs):
            atomic(*args, **kwargs)
            raise RuntimeError('after durable config replace')
        with patch.object(mode.control_entry, 'atomic', interrupted), self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(json.loads((self.root / mode.RECORD).read_text())['phase'], 'stopped')
        self.assertEqual(json.loads(self.config_path.read_text())['automatic_delivery'], 'scheduled_only')
        self.invoke()
        self.assertEqual(self.trace.count('activated'), 1)
        self.assertEqual(self.ledger, self.initial_ledger)

    def test_ci_source_failure_happens_before_any_stop(self):
        with self.assertRaisesRegex(ValueError, 'CI'):
            mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                         self.factory, source_check=lambda _: (_ for _ in ()).throw(ValueError('CI')),
                         inspect=self.inventory)
        self.assertEqual(self.trace, [])
        self.assertFalse((self.root / mode.RECORD).exists())

    def test_container_replacement_during_stop_is_rejected(self):
        self.fail = 'stopped'
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.container['Id'] = 'unexpected'
        with self.assertRaisesRegex(ServiceError, 'old_container_changed'):
            self.invoke()
        self.assertNotIn('started', self.trace)

    def test_normal_deploy_is_blocked_before_staging_during_transition(self):
        from deploy.stage_helper import execute
        self.fail = 'stopped'
        with self.assertRaises(RuntimeError):
            self.invoke()
        with self.assertRaisesRegex(ServiceError, 'forward_recovery'):
            execute(('deploy', '11', 'c' * 40, 'd' * 64), {'live_enabled': True}, self.root,
                    stage_runner=lambda *a: self.fail('must not stage'))

    def delayed_socket_factory(self, *args):
        driver = self.factory(*args)
        ready, status = driver.wait_ready, driver.status
        listening = False
        def wait_ready(release):
            nonlocal listening
            ready(release)
            listening = True
        def socket_status(release):
            if not listening:
                raise ServiceError('docker_operation_failed')
            return status(release)
        driver.wait_ready, driver.status = wait_ready, socket_status
        return driver

    def test_new_container_waits_for_control_socket_before_status(self):
        result = mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                              self.delayed_socket_factory, source_check=lambda *args, **kwargs: None,
                              inspect=self.inventory)
        self.assertEqual(result['status'], 'scheduled_only')
        self.assertEqual(self.trace.count('activated'), 1)

    def test_configured_recovery_waits_for_control_socket(self):
        self.fail = 'started'
        with self.assertRaises(RuntimeError):
            self.invoke()
        result = mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                              self.delayed_socket_factory, source_check=lambda *args, **kwargs: None,
                              inspect=self.inventory)
        self.assertEqual(result['status'], 'scheduled_only')
        self.assertEqual(self.trace.count('started'), 1)
        self.assertEqual(self.trace.count('activated'), 1)

    def test_status_authentication_failure_is_not_caught_or_activated(self):
        def factory(*args):
            driver = self.factory(*args)
            def unauthorized(_):
                raise ServiceError('control_rejected')
            driver.status = unauthorized
            return driver
        with self.assertRaisesRegex(ServiceError, 'control_rejected'):
            mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                         factory, source_check=lambda *args, **kwargs: None, inspect=self.inventory)
        self.assertNotIn('activated', self.trace)
        self.assertEqual(json.loads((self.root / mode.RECORD).read_text())['phase'], 'configured')

    def source_fixtures(self):
        proof = {'commit': SHA, 'main': SHA, 'run_id': 123, 'run_attempt': 1}
        tree = {}
        for name in mode.SOURCE_FILES:
            data = (Path(mode.__file__).parent / name).read_bytes()
            tree[name] = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        return proof, tree

    def check_main_advance_recovery(self, failure):
        proof, tree = self.source_fixtures()
        with patch.object(mode.control_entry, 'trusted_candidate', return_value=proof) as trust, \
                patch.object(mode.control_entry, 'source_tree', return_value=tree):
            self.fail = failure
            with self.assertRaises(RuntimeError):
                mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                             self.factory, inspect=self.inventory)
            self.assertEqual(trust.call_count, 2)
            record = json.loads((self.root / mode.RECORD).read_text())
            self.assertEqual(record['source']['ci'], proof)
            trust.side_effect = ValueError('control_target_superseded')
            result = mode.migrate(self.command, {'root': str(self.root)}, self.root, self.policy,
                                  self.factory, inspect=self.inventory)
            self.assertEqual(result['status'], 'scheduled_only')
            self.assertEqual(trust.call_count, 2)
            self.assertEqual(self.ledger, self.initial_ledger)

    def test_prepared_recovery_uses_pinned_source_after_main_advances(self):
        self.check_main_advance_recovery('stopped')

    def test_configured_recovery_uses_pinned_source_after_main_advances(self):
        self.check_main_advance_recovery('started')

    def test_pinned_source_changed_or_missing_evidence_is_rejected(self):
        proof, tree = self.source_fixtures()
        with patch.object(mode.control_entry, 'trusted_candidate', return_value=proof), \
                patch.object(mode.control_entry, 'source_tree', return_value=tree):
            evidence = mode.verify_source(SHA)
        evidence['files']['stage_helper.py'] = '0' * 64
        with self.assertRaisesRegex(ServiceError, 'source_changed'):
            mode.verify_source(SHA, evidence, recovery=True)
        with self.assertRaisesRegex(ServiceError, 'evidence_invalid'):
            mode.verify_source(SHA, None, recovery=True)
        evidence['ci']['commit'] = 'c' * 40
        with self.assertRaisesRegex(ServiceError, 'ci_evidence_invalid'):
            mode.verify_source(SHA, evidence, recovery=True)

    def test_inventory_escapes_literal_dots_in_docker_names(self):
        commands = []
        def command(arguments):
            commands.append(arguments)
            return SimpleNamespace(stdout=b'')
        driver = SimpleNamespace(live={'container': 'ai.brief', 'legacy_container': 'legacy.v1'},
                                 command=command)
        self.assertIsNone(mode.inventory(driver, SHA, {self.old_binding}))
        for arguments, exact, other in zip(commands, ('/ai.brief', '/legacy.v1'),
                                           ('/ai-brief', '/legacy-v1')):
            expression = arguments[arguments.index('--filter') + 1].removeprefix('name=')
            self.assertIsNotNone(re.fullmatch(expression, exact))
            self.assertIsNone(re.fullmatch(expression, other))

    def test_existing_readiness_retries_transient_socket_startup(self):
        from deploy.supervisor import DockerDriver
        from unittest.mock import Mock
        driver = self.factory()
        driver.status = Mock(side_effect=[ServiceError('docker_operation_failed'),
                                          ServiceError('docker_operation_failed'),
                                          {'phase': 'paused', 'reconciled': True, 'in_flight': False}])
        with patch('deploy.supervisor.time.sleep') as sleep:
            DockerDriver.wait(driver, SHA)
        self.assertEqual(driver.status.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_command_has_no_mode_rollback_or_arbitrary_path(self):
        self.assertEqual(parse_command(' '.join(self.command)), self.command)
        for name in ('legacy', 'scheduled-only /tmp/config', 'scheduled-only 10 ' + SHA + ' legacy'):
            with self.assertRaises(ValueError):
                parse_command(name)

    def test_guard_accepts_only_complete_bound_verifier_maintenance_chain(self):
        base = {**self.original, 'automatic_delivery':'scheduled_only',
                'verifier_path':str(mode.scheduler_service.VERIFIERS / ('c'*40) / 'scripts/verify-scheduled-delivery.mjs')}
        runtime_binding = lambda config: hashlib.sha256(('runtime-files-and-node:'+json.dumps(config,sort_keys=True)).encode()).hexdigest()
        write_json(self.root / mode.RECORD, {'phase':'complete', 'old_config':base, 'new_binding':runtime_binding(base)})
        maintenance = self.root / 'maintenance'

        def proof(original, sha):
            candidate = {**original, 'verifier_path':str(mode.scheduler_service.VERIFIERS / sha / 'scripts/verify-scheduled-delivery.mjs')}
            folder = maintenance / sha; folder.mkdir(parents=True)
            write_json(folder / 'original.json', original); write_json(folder / 'candidate.json', candidate)
            prepared = {'reports':sha, 'application':SHA, 'old_binding':runtime_binding(original),
                        'original_config_sha256':hashlib.sha256((folder/'original.json').read_bytes()).hexdigest(),
                        'candidate_config_sha256':hashlib.sha256((folder/'candidate.json').read_bytes()).hexdigest()}
            write_json(folder/'prepared.json', prepared)
            activation = {'reports':sha, 'application':SHA, 'old_binding':runtime_binding(original),
                          'new_binding':runtime_binding(candidate), 'phase':'complete',
                          'original_records_preserved':True, 'application_policy_unchanged':True}
            write_json(folder/'activation.json', activation)
            return candidate, folder, activation

        first, _, _ = proof(base, 'd'*40)
        current, folder, activation = proof(first, 'e'*40)
        write_json(self.config_path, current)
        before = (self.root/mode.RECORD).read_bytes()
        with patch.object(mode, 'VERIFIER_MAINTENANCE', maintenance):
            mode.guard(self.root)
            for change in ({'phase':'activation_started'}, {'original_records_preserved':False},
                           {'new_binding':'bad'}, {'old_binding':'0'*64}, {'application':'f'*40}):
                write_json(folder/'activation.json', {**activation, **change})
                with self.assertRaisesRegex(ServiceError, 'migration_mode_regression'): mode.guard(self.root)
            write_json(folder/'activation.json', activation)
            write_json(self.config_path, {**current, 'publisher_uid':123})
            with self.assertRaisesRegex(ServiceError, 'migration_mode_regression'): mode.guard(self.root)
            write_json(self.config_path, current)
            (folder/'original.json').write_text('{}')
            with self.assertRaisesRegex(ServiceError, 'migration_mode_regression'): mode.guard(self.root)
        self.assertEqual((self.root/mode.RECORD).read_bytes(), before)
