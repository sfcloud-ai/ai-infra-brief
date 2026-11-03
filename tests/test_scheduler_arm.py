"""首次启用准备的构造回归；不会调用 Docker 或真实发送。"""
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from deploy.arm_scheduler import arm, administrative_locks
from deploy.policy import Policy


class ArmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'stage.json'
        self.config.write_text(json.dumps({'live_enabled': True, 'live': {'notice_service': True}}))
        self.config.chmod(0o600)
        self.release, self.binding = 'a' * 40, 'b' * 64
        Policy(self.root).completed(self.release)
        self.driver = Mock()
        self.driver.scheduler_binding.return_value = self.binding
        self.driver.current_sender.return_value = self.release
        self.driver.status.return_value = {'release': self.release, 'phase': 'active', 'in_flight': False,
                                           'reconciled': True, 'delivery_ready': True}
        for name in ('root_owned_file', 'docker_command'):
            patcher = patch('deploy.arm_scheduler.' + name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, release=None, binding=None):
        return arm(release or self.release, binding or self.binding, self.config, self.root,
                   factory=lambda config, root: self.driver)

    def test_arm_only_prepares_future_release_and_preserves_mode(self):
        value = self.call()
        self.assertFalse(value['activation_performed'])
        self.assertEqual(value['status'], 'prepared_for_next_release')
        self.assertEqual(json.loads(self.config.read_text())['live'], {'notice_service': True, 'scheduler': True})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Policy(self.root).value['active'], self.release)
        self.assertEqual([c[0] for c in self.driver.method_calls],
                         ['scheduler_binding', 'preflight', 'preflight_rollback', 'status', 'current_sender'])

    def test_failed_preflight_never_changes_switch(self):
        before = self.config.read_bytes()
        self.driver.preflight.side_effect = ValueError('incompatible rollback')
        with self.assertRaises(ValueError):
            self.call()
        self.assertEqual(self.config.read_bytes(), before)
        self.driver.status.assert_not_called()

    def test_pending_operation_or_changed_release_rejected(self):
        before = self.config.read_bytes()
        for name in ('switch-pending.json', 'control-pending.json'):
            pending = self.root / name
            pending.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'pending_operation'):
                self.call()
            pending.unlink()
        with self.assertRaisesRegex(ValueError, 'active_release_changed'):
            self.call(release='c' * 40)
        self.assertEqual(self.config.read_bytes(), before)
        self.driver.preflight.assert_not_called()

    def test_scope_and_nonhealthy_states_cannot_arm(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, 'binding_changed'):
            self.call(binding='c' * 64)
        for key, value in [('phase', 'paused'), ('in_flight', True), ('reconciled', False),
                           ('delivery_ready', False), ('release', 'c' * 40)]:
            with self.subTest(key=key):
                original = self.driver.status.return_value.copy()
                self.driver.status.return_value[key] = value
                with self.assertRaisesRegex(ValueError, 'not_drained_and_healthy'):
                    self.call()
                self.driver.status.return_value = original
        self.assertEqual(self.config.read_bytes(), before)

    def test_input_change_during_read_does_not_get_overwritten(self):
        def mutate(*args):
            self.config.write_text('{"changed":true}')
        self.driver.preflight.side_effect = mutate
        with self.assertRaisesRegex(ValueError, 'inputs_changed'):
            self.call()
        self.assertEqual(self.config.read_text(), '{"changed":true}')

    def test_duplicate_config_and_invalid_identity_rejected(self):
        before = self.config.read_bytes()
        for release, binding in [('a', self.binding), (self.release, 'b')]:
            with self.assertRaisesRegex(ValueError, 'exact_scheduler'):
                self.call(release, binding)
        self.assertEqual(self.config.read_bytes(), before)
        self.config.write_text('{"live_enabled":true,"live_enabled":false}')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.call()

    def test_real_controller_lock_blocks_before_creating_application_lock(self):
        import fcntl
        outer = self.root / 'control-entry.lock'
        fd = os.open(outer, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('deploy.arm_scheduler.admin_path'), self.assertRaises(BlockingIOError):
                with administrative_locks(self.root):
                    self.fail('contended control transaction must not enter')
            self.assertFalse((self.root / 'deployment.lock').exists())
        finally:
            os.close(fd)

    def test_real_cross_process_lock_excludes_versioned_entry_until_exit(self):
        code = ('import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR);\n'
                'try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
                'except BlockingIOError: sys.exit(17)\n')
        outer = self.root / 'control-entry.lock'
        with patch('deploy.arm_scheduler.admin_path'):
            with administrative_locks(self.root):
                result = subprocess.run([sys.executable, '-c', code, str(outer)], timeout=10)
                self.assertEqual(result.returncode, 17)
                self.assertTrue((self.root / 'deployment.lock').exists())
            result = subprocess.run([sys.executable, '-c', code, str(outer)], timeout=10)
            self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
