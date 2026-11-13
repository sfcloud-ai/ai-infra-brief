"""调度控制层的配置、隔离与切换回归；Docker 故障使用注入替身。"""

import copy
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deploy import scheduler_service as service
from deploy.supervisor import DockerDriver, ServiceError, Supervisor, CutoverDeferred
from deploy.policy import Policy
from deploy.stage_helper import execute
from test_cd_control import FakeDriver
from integrations.slack_wecom.runtime import ControlledRelay
from integrations.slack_wecom import relay


class SchedulerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'scheduler.json'
        self.ipc = self.root / 'ipc'
        self.ipc.mkdir(mode=0o711)
        self.ipc.chmod(0o711)
        self.node = self.root / 'node'
        self.node.write_bytes(b'synthetic executable')
        self.node.chmod(0o755)
        self.verifiers = self.root / 'verifiers'
        self.tree = self.verifiers / ('a' * 40)
        (self.tree / 'scripts').mkdir(parents=True)
        self.script = self.tree / 'scripts/verify-scheduled-delivery.mjs'
        self.script.write_text('export const synthetic = true;')
        self.value = {'activation_date': '2026-11-04', 'publisher_uid': os.getuid() + 1,
                      'socket_path': str(self.ipc / 'scheduled.sock'), 'node_path': str(self.node),
                      'verifier_path': str(self.script)}
        for name, value in [('CONFIG', self.config), ('IPC', self.ipc), ('NODE', self.node), ('VERIFIERS', self.verifiers)]:
            patcher = patch.object(service, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def load(self, value=None):
        self.config.write_text(json.dumps(self.value if value is None else value))
        # 构造测试只替换 root 所有权检查，独立用例验证权限拒绝条件。
        with patch.object(service, 'admin_path', side_effect=lambda p, **kw: p.lstat()):
            return service.SchedulerDeployment(os.getuid(), os.getgid())

    def test_pins_all_dependencies_and_preserves_fixed_mounts(self):
        first = self.load()
        dependency = self.tree / 'dependency.mjs'
        dependency.write_text('export const synthetic = 1;')
        second = self.load()
        self.assertNotEqual(first.binding, second.binding)
        self.assertEqual(second.binding, self.load().binding)
        mounts = second.mounts()
        self.assertIn('type=bind,src=' + str(self.ipc) + ',dst=' + str(self.ipc), mounts)
        for path in [self.config, self.node, self.tree]:
            self.assertIn('type=bind,src=' + str(path) + ',dst=' + str(path) + ',readonly', mounts)
        self.assertTrue(all(v.endswith(',readonly') for v in second.mounts(True)[1::2]))

    def test_scheduled_only_is_explicit_and_changes_deployment_binding(self):
        original = self.load()
        selected = self.load({**self.value, 'automatic_delivery': 'scheduled_only'})
        self.assertNotEqual(original.binding, selected.binding)
        for mode in (None, True, 'legacy', 'scheduled-only', ''):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'scheduled_only_mode'):
                self.load({**self.value, 'automatic_delivery': mode})

    def test_rejects_external_paths_fields_uid_date_and_config_duplicates(self):
        changes = [{'destination': 'other'}, {'publisher_uid': os.getuid()}, {'publisher_uid': True},
                   {'publisher_uid': 0}, {'activation_date': '2026-02-30'},
                   {'socket_path': str(self.ipc / 'check.sock')}, {'node_path': '/other/node'},
                   {'verifier_path': '/other/scripts/verify-scheduled-delivery.mjs'},
                   {'verifier_path': str(self.tree / 'scripts/other.mjs')}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                self.load({**self.value, **change})
        self.config.write_text('{"activation_date":"2026-11-04","activation_date":"2026-11-05"}')
        with patch.object(service, 'admin_path'), self.assertRaisesRegex(ValueError, 'duplicate'):
            service.SchedulerDeployment(os.getuid(), os.getgid())

    def test_rejects_wrong_ipc_mode_and_nonexecutable_node(self):
        for mode in (0o700, 0o777):
            self.ipc.chmod(mode)
            with self.assertRaisesRegex(ValueError, 'ipc_directory'):
                self.load()
        self.ipc.chmod(0o711)
        self.node.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'not_executable'):
            self.load()

    def test_admin_path_rejects_links_writable_files_and_ancestors(self):
        path = Mock()
        path.is_absolute.return_value = True
        path.resolve.return_value = path
        parent = Mock()
        parent.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        path.parents = [parent]
        base = {'st_mode': stat.S_IFREG | 0o444, 'st_uid': 0, 'st_nlink': 1}
        path.lstat.return_value = SimpleNamespace(**base)
        service.admin_path(path)
        for change in [{'st_uid': 1000}, {'st_mode': stat.S_IFREG | 0o664},
                       {'st_mode': stat.S_IFLNK | 0o777}, {'st_nlink': 2}]:
            path.lstat.return_value = SimpleNamespace(**{**base, **change})
            with self.assertRaises(ValueError):
                service.admin_path(path)
        path.lstat.return_value = SimpleNamespace(**base)
        parent.lstat.return_value.st_mode |= 0o020
        with self.assertRaisesRegex(ValueError, 'parent'):
            service.admin_path(path)

    def test_preflight_never_mounts_ledger_credentials_or_network(self):
        scheduler = self.load()
        driver = Mock(config={'uid': 1000, 'gid': 1000, 'image': 'synthetic-image', 'root': '/synthetic-releases'})
        driver.code.return_value = '/prototype/releases/' + 'b' * 40 + '/code'
        scheduler.preflight(driver, 'b' * 40)
        args = driver.command.call_args_list[0].args[0]
        self.assertEqual(args[args.index('--network') + 1], 'none')
        self.assertNotIn('dst=/relay', ' '.join(args))
        self.assertTrue(all(arg.endswith(',readonly') for arg in args if arg.startswith('type=bind')))
        self.assertIn('scheduler_config', args[args.index('-c') + 1])
        self.assertIn('SCHEDULED_ONLY_VERSION', args[args.index('-c') + 1])
        self.assertIn('await import', args[args.index('-c') + 1])
        name = args[args.index('--name') + 1]
        self.assertEqual(driver.command.call_args_list[-1].args[0], ['rm', '-f', name])
        driver.command.side_effect = [ServiceError('incompatible'), None]
        with self.assertRaises(ServiceError):
            scheduler.preflight(driver, 'b' * 40)
        self.assertEqual(driver.command.call_args.args[0][:2], ['rm', '-f'])

    def test_dependency_import_does_not_run_verifier_cli_guard(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('此用例需要 Node；Linux 私有验证环境必须运行。')
        self.script.write_text("import {pathToFileURL} from 'node:url';\n"
                               "if(import.meta.url===pathToFileURL(process.argv[1]||'').href)process.exit(19);\n")
        result = subprocess.run([node, '--input-type=module', '-e', service.IMPORT_VERIFIER,
                                 self.script.as_uri()], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_revision_capability_rejects_old_runtime_without_ledger_mount(self):
        scheduler = self.load()
        driver = Mock(config={'uid': 1000, 'gid': 1000, 'image': 'synthetic-image', 'root': '/synthetic-releases'})
        driver.code.return_value = '/synthetic/code'
        scheduler.preflight(driver, 'b' * 40, require_revision_notices=True)
        args = driver.command.call_args_list[0].args[0]
        check = args[args.index('-c') + 1]
        self.assertNotIn('dst=/relay', ' '.join(args))
        runtime = SimpleNamespace(serve=lambda scheduler_config: None, SCHEDULER_DRAIN_VERSION=1)
        modules = {name: SimpleNamespace() for name in ('scheduled_runtime', 'scheduled_delivery', 'scheduled_socket')}
        with patch.dict('sys.modules', {'runtime': runtime, **modules}), patch('sys.argv', ['check', '/synthetic/code']):
            with self.assertRaisesRegex(AssertionError, 'revision_ledger_requires_compatible_release'):
                exec(check, {})

    def test_revision_ledger_gate_is_read_only_and_precedes_container_changes(self):
        directory = self.root / 'relay/state'; directory.mkdir(parents=True)
        path = directory / 'state.sqlite3'
        with sqlite3.connect(path) as database:
            database.execute('PRAGMA journal_mode=WAL')
            database.execute('CREATE TABLE requests(mode TEXT)')
            database.execute('CREATE TABLE scheduled_revision_notices(request_id TEXT)')
        database.close()
        driver = DockerDriver.__new__(DockerDriver)
        driver.config = {'uid': os.getuid(), 'gid': os.getgid()}
        driver.live = {'relay_root': str(directory.parent), 'container': 'managed', 'legacy_container': 'legacy'}
        self.assertFalse(driver.revision_records_present())
        with sqlite3.connect(path) as database:
            database.execute("INSERT INTO scheduled_revision_notices VALUES ('morning-20261101-r2')")
        database.close()
        self.assertFalse(path.with_name(path.name + "-wal").exists())
        before = path.read_bytes()
        self.assertTrue(driver.revision_records_present())
        driver.scheduler = Mock(); driver.scheduler.preflight.side_effect = ServiceError('incompatible_revision_runtime')
        driver.running_release = Mock(return_value=None); driver.inspect = Mock(return_value=None); driver.command = Mock()
        with self.assertRaisesRegex(ServiceError, 'incompatible_revision_runtime'):
            driver.start_paused('b' * 40)
        driver.command.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        for item in directory.iterdir():
            self.assertEqual(item.stat().st_uid, os.getuid())

    def test_page_ledger_requires_version_two_for_candidate_and_rollback(self):
        directory=self.root/'relay/state';directory.mkdir(parents=True)
        path=directory/'state.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE scheduled_notices(record TEXT)')
            db.execute('INSERT INTO scheduled_notices VALUES (?)',(json.dumps({'page_state':{'pending':[]}}),))
        driver=DockerDriver.__new__(DockerDriver)
        driver.config={'uid':os.getuid(),'gid':os.getgid()}
        driver.live={'relay_root':str(directory.parent),'container':'managed','legacy_container':'legacy'}
        self.assertEqual(driver.revision_records_present(),2)
        driver.scheduler=Mock();driver.scheduler.preflight.side_effect=ServiceError('page_release_incompatible')
        driver.running_release=Mock(return_value=None);driver.inspect=Mock(return_value=None);driver.command=Mock()
        with self.assertRaisesRegex(ServiceError,'page_release_incompatible'):driver.start_paused('b'*40)
        driver.scheduler.preflight.assert_called_once_with(driver,'b'*40,require_revision_notices=2)
        driver.command.assert_not_called()
        scheduler=self.load()
        candidate=Mock(config={'uid':1000,'gid':1000,'image':'synthetic','root':'/releases'})
        candidate.code.return_value='/code'
        scheduler.preflight(candidate,'c'*40,require_revision_notices=2)
        args=candidate.command.call_args_list[0].args[0];check=args[args.index('-c')+1]
        runtime=SimpleNamespace(serve=lambda scheduler_config:None,SCHEDULER_DRAIN_VERSION=1,REVISION_NOTICES_VERSION=1)
        modules={name:SimpleNamespace() for name in ('scheduled_runtime','scheduled_delivery','scheduled_socket')}
        with patch.dict('sys.modules',{'runtime':runtime,**modules}),patch('sys.argv',['check','/code']):
            with self.assertRaisesRegex(AssertionError,'revision_ledger_requires_compatible_release'):exec(check,{})

    def container(self, scheduler):
        mounts = [{'Destination': str(p), 'Source': str(p), 'RW': p == self.ipc}
                  for p in [self.config, self.node, self.tree, self.ipc]]
        mounts += [{'Destination': p, 'Source': '/synthetic' + p, 'RW': True} for p in ['/relay/state', '/relay/run']]
        return {'Config': {'User': str(os.getuid()) + ':' + str(os.getgid()),
                           'Labels': {service.LABEL: scheduler.binding}, 'Cmd': ['serve'] + scheduler.serve_args()},
                'HostConfig': {'ReadonlyRootfs': True, 'Privileged': False, 'CapDrop': ['ALL'],
                               'SecurityOpt': ['no-new-privileges'], 'UsernsMode': '', 'PortBindings': {}},
                'Mounts': mounts}

    def test_health_rejects_binding_permission_mount_or_peer_identity_drift(self):
        scheduler = self.load()
        original = self.container(scheduler)
        scheduler.assert_container(original)
        mutations = [lambda v: v['Config']['Labels'].clear(), lambda v: v['Config'].update(User='0:0'),
                     lambda v: v['Config'].update(Cmd=['serve']), lambda v: v['Mounts'][0].update(RW=True),
                     lambda v: v['HostConfig'].update(GroupAdd=['extra']),
                     lambda v: v['Mounts'].append({'Destination': '/other', 'Source': '/other', 'RW': True})]
        for mutate in mutations:
            item = copy.deepcopy(original)
            mutate(item)
            with self.assertRaisesRegex(ValueError, 'binding_changed'):
                scheduler.assert_container(item)


class SchedulerCutoverTests(unittest.TestCase):
    def test_restricted_helper_reports_deferred_without_claiming_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = FakeDriver()
            driver.quiesce = Mock(side_effect=CutoverDeferred('queued'))
            result = execute(('deploy', '10', 'b' * 40, 'd' * 64), {'live_enabled': True}, directory,
                             stage_runner=lambda *args: None, driver_factory=lambda *args: driver)
            self.assertEqual(result, {'status': 'deferred', 'commit': 'b' * 40})
            self.assertEqual(driver.running, driver.legacy)
            self.assertEqual(Policy(directory).value['active'], driver.legacy)
            self.assertFalse((Path(directory) / 'switch-pending.json').exists())

    def test_restarted_process_requires_fresh_pause_after_revocation(self):
        controller, db = self.controller()
        driver = DockerDriver.__new__(DockerDriver)
        driver.scheduler = Mock()
        driver.live = {'container': 'synthetic-managed'}
        driver.running_release = Mock(return_value=controller.release)
        operations = []
        with tempfile.TemporaryDirectory() as directory:
            driver.activation = Path(directory)
            active = driver.activation / 'active.json'
            active.write_text(json.dumps({'release': controller.release}))
            def status(release, operation):
                operations.append(operation)
                if operation == 'drain':
                    result = self.request(controller, operation)
                    # 模拟旧确认返回后重启并准入新任务；不能用旧确认直接停止。
                    controller.phase = 'active'
                    db.execute("INSERT INTO scheduled_notices(phase) VALUES ('pending')")
                    return result
                self.assertEqual(json.loads(active.read_text()), {'release': None})
                return self.request(controller, operation)
            with patch.object(driver, 'status', side_effect=status), patch.object(driver, 'command') as command:
                with self.assertRaisesRegex(ServiceError, 'unreconciled_delivery_blocks_stop'):
                    driver.quiesce(controller.release)
                self.assertEqual(operations, ['drain', 'pause'])
                command.assert_not_called()

    def controller(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        for table in ('requests', 'parts', 'image_parts'):
            db.execute('CREATE TABLE ' + table + ' (status TEXT)')
        db.execute('CREATE TABLE scheduled_notices (phase TEXT, request_id TEXT)')
        receiver = SimpleNamespace(store=SimpleNamespace(db=db))
        scheduler = SimpleNamespace(assert_reconciled=Mock(), queue=Mock())
        controller = ControlledRelay(relay, receiver, 'a' * 40, scheduled=scheduler)
        controller.phase = 'active'
        controller.polls = 1
        return controller, db

    def request(self, controller, operation):
        return controller.dispatch('/control/' + operation,
                                   {'release': controller.release, 'deadline': time.monotonic() + 60,
                                    'nonce': None}, os.getuid())

    def test_pending_or_retry_keeps_sender_active_when_cd_defers(self):
        controller, db = self.controller()
        driver = DockerDriver.__new__(DockerDriver)
        driver.scheduler = Mock()
        driver.live = {'container': 'synthetic-managed'}
        driver.running_release = Mock(return_value=controller.release)
        driver.status = lambda release, operation: self.request(controller, operation)
        with tempfile.TemporaryDirectory() as directory:
            policy = Policy(directory)
            policy.completed(controller.release)
            driver.activation = Path(directory)
            active = driver.activation / 'active.json'
            active.write_text(json.dumps({'release': controller.release}))
            for phase in ('pending', 'verifying', 'unknown'):
                db.execute('DELETE FROM scheduled_notices')
                db.execute('INSERT INTO scheduled_notices(phase) VALUES (?)', (phase,))
                with self.subTest(phase=phase), \
                        patch.object(driver, 'preflight'), patch.object(driver, 'preflight_rollback'), \
                        patch.object(driver, 'scheduler_binding', return_value='c' * 64), \
                        patch.object(driver, 'revoke_permit') as revoke, patch.object(driver, 'command') as command:
                    with self.assertRaises(CutoverDeferred):
                        Supervisor(directory, driver, policy).switch('b' * 40)
                    self.assertEqual(controller.phase, 'active')
                    controller.tick_scheduled()
                    self.assertFalse((Path(directory) / 'switch-pending.json').exists())
                    self.assertEqual(json.loads(active.read_text()), {'release': controller.release})
                    revoke.assert_not_called()
                    command.assert_not_called()
            self.assertEqual(controller.scheduled.queue.tick.call_count, 3)

    def test_atomic_drain_closes_admission_only_when_no_jobs_remain(self):
        controller, db = self.controller()
        db.execute("INSERT INTO scheduled_notices(phase) VALUES ('pending')")
        self.assertFalse(self.request(controller, 'drain')['reconciled'])
        self.assertEqual(controller.phase, 'active')
        db.execute("UPDATE scheduled_notices SET phase='complete'")
        result = self.request(controller, 'drain')
        self.assertEqual(result['phase'], 'paused')
        self.assertTrue(result['reconciled'])
        controller.tick_scheduled()
        controller.scheduled.queue.tick.assert_not_called()

    def test_uncertain_drain_keeps_pending_and_never_stops_or_replaces_sender(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Policy(directory)
            policy.completed('a' * 40)
            driver = DockerDriver.__new__(DockerDriver)
            driver.scheduler = Mock(binding='c' * 64)
            driver.live = {'container': 'synthetic-managed'}
            with patch.object(driver, 'preflight'), patch.object(driver, 'preflight_rollback'), \
                    patch.object(driver, 'current_sender', return_value='a' * 40), \
                    patch.object(driver, 'running_release', return_value='a' * 40), \
                    patch.object(driver, 'status', side_effect=ServiceError('lost_reply')), \
                    patch.object(driver, 'command') as command, patch.object(driver, 'revoke_permit') as revoke:
                with self.assertRaisesRegex(ServiceError, 'lost_reply'):
                    Supervisor(directory, driver, policy).switch('b' * 40)
                self.assertTrue((Path(directory) / 'switch-pending.json').exists())
                command.assert_not_called()
                revoke.assert_not_called()

    def test_active_health_accepts_runnable_queue_but_not_invalid_state(self):
        controller, db = self.controller()
        db.execute("INSERT INTO scheduled_notices(phase) VALUES ('pending')")
        result = self.request(controller, 'status')
        self.assertFalse(result['reconciled'])
        self.assertTrue(result['delivery_ready'])
        driver = DockerDriver.__new__(DockerDriver)
        driver.scheduler = Mock()
        with patch.object(driver, 'assert_scheduler_binding'), patch.object(driver, 'status', return_value=result):
            driver.wait(controller.release, active=True)
        controller.scheduled.assert_reconciled.side_effect = ValueError('corrupt')
        self.assertFalse(self.request(controller, 'status')['delivery_ready'])

    def test_interrupted_recovery_checks_scope_before_abort_or_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Policy(directory)
            driver = FakeDriver()
            pending = Path(directory) / 'switch-pending.json'
            pending.write_text(json.dumps({'before': 'a' * 40, 'target': 'b' * 40,
                                           'activation_attempted': False, 'scheduler_binding': 'c' * 64}))
            driver.preflight_recovery = Mock(side_effect=ServiceError('scope_changed'))
            with self.assertRaisesRegex(ServiceError, 'scope_changed'):
                Supervisor(directory, driver, policy).rollback_interrupted()
            self.assertEqual(driver.events, [])
            self.assertTrue(pending.exists())

    def test_recovery_without_container_still_requires_saved_binding(self):
        driver = DockerDriver.__new__(DockerDriver)
        for saved, current in [('a' * 64, None), ('a' * 64, 'b' * 64), (None, 'a' * 64)]:
            driver.scheduler = Mock(binding=current) if current is not None else None
            with patch.object(driver, 'preflight_rollback') as preflight:
                with self.assertRaisesRegex(ServiceError, 'recovery_scope_changed'):
                    driver.preflight_recovery({'before': 'c' * 40, 'scheduler_binding': saved})
                preflight.assert_not_called()
        driver.scheduler = Mock(binding='a' * 64)
        with patch.object(driver, 'preflight_rollback') as preflight:
            driver.preflight_recovery({'before': 'c' * 40, 'scheduler_binding': 'a' * 64})
            preflight.assert_called_once_with('c' * 40)

    def test_bad_rollback_runtime_is_rejected_before_old_service_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = Policy(directory)
            driver = FakeDriver()
            policy.completed(driver.legacy)
            driver.preflight_rollback = Mock(side_effect=ServiceError('unsupported_previous'))
            with self.assertRaisesRegex(ServiceError, 'unsupported_previous'):
                Supervisor(directory, driver, policy).switch('a' * 40)
            self.assertEqual(driver.running, driver.legacy)
            self.assertEqual(driver.events, [])
            self.assertFalse((Path(directory) / 'switch-pending.json').exists())

    def test_start_and_rollback_use_same_mounts_config_and_paused_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = DockerDriver.__new__(DockerDriver)
            driver.live = {'container': 'synthetic-managed', 'legacy_container': 'synthetic-old', 'relay_root': directory}
            driver.config = {'image': 'synthetic-image'}
            driver.activation = Path(directory)
            driver.scheduler = Mock(binding='c' * 64)
            driver.scheduler.serve_args.return_value = ['--scheduler-config', '/fixed/config.json']
            with patch.object(driver, 'running_release', return_value=None), \
                    patch.object(driver, 'inspect', return_value=None), patch.object(driver, 'common', return_value=['fixed-mount']), \
                    patch.object(driver, 'runtime', return_value='/fixed/runtime.py'), \
                    patch.object(driver, 'code', return_value='/fixed/code'), patch.object(driver, 'command') as command:
                for release in ['a' * 40, 'b' * 40, 'a' * 40]:
                    driver.start_paused(release)
                    args = command.call_args.args[0]
                    self.assertIn('fixed-mount', args)
                    self.assertIn(service.LABEL + '=' + 'c' * 64, args)
                    self.assertEqual(args[-2:], ['--scheduler-config', '/fixed/config.json'])
                    self.assertIn('--activation-file', args)
                    self.assertNotIn('activate', args)

    def test_cannot_silently_disable_or_change_a_running_scheduler(self):
        driver = DockerDriver.__new__(DockerDriver)
        driver.live = {'container': 'synthetic-managed'}
        item = {'Config': {'Labels': {service.LABEL: 'a' * 64}}}
        for scheduler in [None, Mock(binding='b' * 64)]:
            driver.scheduler = scheduler
            with patch.object(driver, 'inspect', return_value=item), patch.object(driver, 'preflight') as preflight:
                with self.assertRaisesRegex(ServiceError, 'scope_change'):
                    driver.preflight_rollback('c' * 40)
                preflight.assert_not_called()

    def test_default_does_not_load_scheduler_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch('deploy.supervisor.SchedulerDeployment') as load:
            path = Path(directory)
            config = {'uid': 1000, 'gid': 1000, 'live': {'container': 'synthetic-managed', 'legacy_container': 'synthetic-old',
                       'relay_root': str(path), 'control_code': str(path), 'legacy_relay': '/legacy/relay.py',
                       'legacy_images': '/legacy/images.py'}}
            self.assertIsNone(DockerDriver(config, path).scheduler)
            load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
