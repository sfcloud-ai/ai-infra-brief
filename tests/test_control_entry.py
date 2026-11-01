"""控制代码升级只接受主线证据，失败时保留业务状态与可核对意图。"""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if os.name == 'posix':
    from deploy import control_entry as control


@unittest.skipUnless(os.name == 'posix', '需要 Linux 受限入口；完整 CI 在 Linux 执行')
class ControlEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.before, self.target, self.latest = 'a' * 40, 'b' * 40, 'c' * 40
        self.ref = self.target
        self.files = []
        self.comparison = 'ahead'
        self.runs = [dict(id=10, run_attempt=1, head_sha=self.target, head_branch='main', event='push',
                         path='.github/workflows/ci.yml', repository={'full_name': control.REPOSITORY},
                         status='completed', conclusion='success')]

    def read(self, path):
        if path == '/git/ref/heads/main':
            return {'object': {'type': 'commit', 'sha': self.ref}}
        if path.startswith('/compare/'):
            before = path.split('/compare/')[1].split('...')[0]
            return {'status': self.comparison, 'base_commit': {'sha': before},
                    'merge_base_commit': {'sha': before}, 'files': self.files}
        return {'workflow_runs': self.runs}

    def test_exact_main_success_and_prompt_only_descendant(self):
        self.assertEqual(control.trusted_candidate(self.target, self.before, self.read)['commit'], self.target)
        self.ref = self.latest
        self.files = [{'filename': 'ai_brief/resources/prompts/editor.md'}]
        self.assertEqual(control.trusted_candidate(self.target, self.before, self.read)['main'], self.latest)

    def test_new_failed_attempt_cannot_reuse_earlier_success(self):
        self.runs.append(dict(self.runs[0], run_attempt=2, conclusion='failure'))
        with self.assertRaisesRegex(ValueError, 'not_successful'):
            control.trusted_candidate(self.target, self.before, self.read)

    def test_wrong_workflow_identity_or_unfinished_ci_cannot_run_code(self):
        for field, value in [('head_sha', self.before), ('head_branch', 'other'), ('event', 'pull_request'),
                             ('path', '.github/workflows/other.yml'), ('repository', {'full_name': 'other/project'}),
                             ('status', 'in_progress')]:
            with self.subTest(field=field):
                previous = self.runs[0][field]
                self.runs[0][field] = value
                with self.assertRaises(control.CandidatePending):
                    control.trusted_candidate(self.target, self.before, self.read)
                self.runs[0][field] = previous

    def test_changed_deployment_input_rename_and_truncated_comparison_reject(self):
        self.ref = self.latest
        for files in ([{'filename': 'deploy/stage_helper.py'}],
                      [{'filename': 'deploy/security-policy.json'}],
                      [{'filename': 'deploy/helpers/defaults.toml'}],
                      [{'filename': 'deploy/verify.sh'}],
                      [{'filename': 'docs/removed.md', 'previous_filename': 'deploy/nested/policy.json'}],
                      [{'filename': 'docs/removed.md', 'previous_filename': 'deploy/runtime.lock'}],
                      [{'filename': 'README.md'}] * 300):
            self.files = files
            with self.assertRaisesRegex(ValueError, 'superseded'):
                control.trusted_candidate(self.target, self.before, self.read)
        self.ref = self.target
        self.comparison = 'diverged'
        with self.assertRaisesRegex(ValueError, 'not_forward'):
            control.trusted_candidate(self.target, self.before, self.read)

    def test_entry_rejects_shell_or_extra_command_arguments(self):
        for arguments in ([], ['status', 'extra'], ['deploy', '1', self.target, 'f' * 64, 'extra'],
                          ['deploy', '-1', self.target, 'f' * 64], ['notice', 'start'], ['sh']):
            with self.assertRaises(ValueError):
                control.parse(arguments)
        self.assertEqual(control.parse(['status']), ('status',))

    def test_deployment_tree_includes_new_modules_but_rejects_links_or_partial_results(self):
        tree = {'truncated': False, 'tree': [dict(path='deploy/' + name, type='blob', mode='100644', sha=self.before)
                                           for name in (*control.FILES, 'new_helpers/config.py')]}
        def read(path):
            return {'tree': {'sha': self.before}} if '/git/commits/' in path else tree
        self.assertIn('new_helpers/config.py', control.source_tree(self.target, read))
        tree['tree'][-1]['mode'] = '120000'
        with self.assertRaisesRegex(ValueError, 'source_file'):
            control.source_tree(self.target, read)
        tree['truncated'] = True
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            control.source_tree(self.target, read)


@unittest.skipUnless(os.name == 'posix', '需要 Linux 受限入口；完整 CI 在 Linux 执行')
class ControlSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before, self.target = 'a' * 40, 'b' * 40
        (self.root / 'code').mkdir()
        self.old = self.write_version(self.before)
        self.new = self.write_version(self.target)
        control.save(self.root / 'control-release.json', self.old)
        control.save(self.root / 'policy.json', {'active': self.before})
        self.command = ('deploy', '10', self.target, 'f' * 64)
        self.guard = patch.object(control, 'admin')
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def write_version(self, commit):
        directory = self.root / 'code' / commit
        directory.mkdir()
        data = b'pass\n'
        for name in control.FILES:
            (directory / name).write_bytes(data)
        return {'commit': commit, 'files': {name: hashlib.sha256(data).hexdigest() for name in control.FILES}}

    def execute(self, call):
        return control.execute(self.command, self.root, prepare=lambda *args: self.new, call=call)

    def test_success_promotes_control_after_application_commit_and_keeps_previous(self):
        ledger = self.root / 'business-ledger'
        ledger.write_bytes(b'never rewind')
        def call(directory, arguments):
            self.assertEqual(directory.name, self.target)
            self.assertEqual(json.loads((self.root / 'control-pending.json').read_text())['phase'], 'invocation_intent')
            control.save(self.root / 'policy.json', {'active': self.target})
            return SimpleNamespace(returncode=0)
        durable = []
        original_sync = control.sync_directory
        def sync(path):
            original_sync(path)
            durable.append((self.root / 'control-pending.json').exists())
        with patch.object(control, 'sync_directory', side_effect=sync):
            self.execute(call)
        self.assertTrue(durable)
        self.assertFalse(durable[-1], '返回成功前必须落盘已删除的意图')
        self.assertEqual(control.current_record(self.root)[0], self.new)
        self.assertEqual(json.loads((self.root / 'control-previous.json').read_text()), self.old)
        self.assertFalse((self.root / 'control-pending.json').exists())
        self.assertEqual(ledger.read_bytes(), b'never rewind')

    def test_confirmed_application_rollback_restores_controller_and_blocks_failed_candidate(self):
        self.execute(lambda *args: SimpleNamespace(returncode=1))
        self.assertEqual(control.current_record(self.root)[0], self.old)
        with self.assertRaisesRegex(ValueError, 'candidate_failed'):
            self.execute(lambda *args: self.fail('不能再次执行失败候选'))

    def test_unknown_call_keeps_intent_and_does_not_retry_after_restart(self):
        def timeout(*args):
            raise TimeoutError('outcome unknown')
        with self.assertRaises(TimeoutError):
            self.execute(timeout)
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            self.execute(lambda *args: self.fail('未知结果只能核对'))
        self.assertTrue((self.root / 'control-pending.json').exists())
        self.assertEqual(control.current_record(self.root)[0], self.new)

    def test_unconfirmed_application_result_keeps_control_intent(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                path = self.root / 'control-pending.json'
                path.unlink(missing_ok=True)
                control.save(self.root / 'control-release.json', self.old)
                def call(*args):
                    if pending:
                        control.save(self.root / 'switch-pending.json', {'target': self.target})
                    return SimpleNamespace(returncode=0)
                with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
                    self.execute(call)
                self.assertTrue(path.exists())

    def test_changed_or_extra_controller_file_never_executes(self):
        directory = self.root / 'code' / self.target
        (directory / 'policy.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'file_changed'):
            self.execute(lambda *args: self.fail('不得执行被修改文件'))
        self.assertFalse((self.root / 'control-pending.json').exists())
        (directory / 'extra.py').write_text('pass')
        with self.assertRaisesRegex(ValueError, 'unexpected_control_file'):
            control.verify_record(self.new, self.root)

    def test_source_main_change_during_download_cannot_promote(self):
        proof = {'commit': self.target, 'main': self.target, 'run_id': 10}
        blobs = {name: hashlib.sha1(b'blob 5\0pass\n').hexdigest() for name in control.FILES}
        with patch.object(control, 'trusted_candidate', side_effect=[proof, dict(proof, main='c' * 40)]), patch.object(control, 'source_tree', return_value=blobs):
            with self.assertRaisesRegex(ValueError, 'inputs_changed'):
                control.stage(self.target, self.before, self.root, download=lambda *args: b'pass\n')
        self.assertEqual(control.current_record(self.root)[0], self.old)

    def test_raw_source_must_match_the_git_tree_blob(self):
        with patch.object(control, 'trusted_candidate', return_value={}), patch.object(control, 'source_tree', return_value={name: 'f' * 40 for name in control.FILES}):
            with self.assertRaisesRegex(ValueError, 'blob_changed'):
                control.stage(self.target, self.before, self.root, download=lambda *args: b'pass\n')
        self.assertEqual(control.current_record(self.root)[0], self.old)

    def test_staged_readonly_code_remains_readable_with_restrictive_umask(self):
        path = self.root / 'controller.py'
        previous = os.umask(0o077)
        try:
            control.atomic(path, b'pass\n', 0o444)
        finally:
            os.umask(previous)
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
