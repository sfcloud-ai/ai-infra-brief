"""单进程定时器的离线回归；本机 IPC 使用注入替身。"""
import contextlib
import copy
import hashlib
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from integrations.slack_wecom import relay, runtime, scheduled_runtime, scheduled_socket
from integrations.slack_wecom.scheduled_delivery import ScheduledDelivery
from integrations.slack_wecom.resume import assert_reconciled
from test_relay_integration import image_fixtures


class ScheduledConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ipc = self.root / "ipc"
        self.ipc.mkdir(mode=0o711)
        self.ipc.chmod(0o711)
        self.node = self.root / "node"
        self.node.write_text("synthetic executable, never run")
        self.node.chmod(0o500)
        self.script = self.root / "verifier.mjs"
        self.script.write_text("synthetic verifier, never run")
        self.script.chmod(0o400)
        self.config = self.root / "scheduler.json"
        self.value = {"activation_date": "2026-11-04", "publisher_uid": os.getuid() + 1,
                      "socket_path": str(self.ipc / "scheduled.sock"),
                      "node_path": str(self.node), "verifier_path": str(self.script)}

    def load(self, changes=None):
        self.config.write_text(json.dumps({**self.value, **(changes or {})}))
        self.config.chmod(0o600)
        return scheduled_runtime.load_config(self.config)

    def test_explicit_complete_config_only(self):
        self.assertEqual(self.load(), self.value)
        with self.assertRaises(ValueError):
            self.load({"enabled": True})
        self.config.write_text(json.dumps({k: v for k, v in self.value.items() if k != "activation_date"}))
        with self.assertRaises(ValueError):
            scheduled_runtime.load_config(self.config)

    def test_scheduled_only_requires_an_explicit_exact_mode(self):
        self.assertEqual(self.load({"automatic_delivery": "scheduled_only"})["automatic_delivery"], "scheduled_only")
        for mode in (None, True, "legacy", "scheduled-only", ""):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "scheduled_only_mode"):
                self.load({"automatic_delivery": mode})

    def test_distinct_positive_publisher_uid(self):
        for uid in (0, -1, True, "1001", os.getuid()):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                self.load({"publisher_uid": uid})

    def test_canonical_activation_boundary(self):
        for value in ("20261104", "2026-1-4", "2026-02-30", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"activation_date": value})

    def test_paths_cannot_select_arbitrary_or_read_only_socket(self):
        for value in ("scheduled.sock", str(self.ipc / "check.sock"), str(self.ipc / "relay.sock")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"socket_path": value})
        for key in ("node_path", "verifier_path"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load({key: "relative"})

    def test_rejects_symlink_or_untrusted_writable_executable(self):
        alias = self.root / "node-alias"
        alias.symlink_to(self.node)
        with self.assertRaises(ValueError):
            self.load({"node_path": str(alias)})
        self.node.chmod(0o777)
        with self.assertRaises(ValueError):
            self.load()

    def test_socket_parent_must_be_owned_and_exactly_0711(self):
        self.ipc.chmod(0o700)
        with self.assertRaises(ValueError):
            self.load()
        self.ipc.chmod(0o777)
        with self.assertRaises(ValueError):
            self.load()

    def test_config_duplicate_fields_or_writable_file_rejected(self):
        self.load()
        self.config.chmod(0o666)
        with self.assertRaises(ValueError):
            scheduled_runtime.load_config(self.config)
        self.config.chmod(0o600)
        self.config.write_text('{"activation_date":"2026-11-04","activation_date":"2026-11-05"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            scheduled_runtime.load_config(self.config)


class ScheduledListenerTests(unittest.TestCase):
    def endpoint(self):
        info = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=os.getuid(), st_dev=1, st_ino=2)
        path = MagicMock()
        path.exists.return_value = True
        path.lstat.return_value = info
        return path, info

    def test_does_not_remove_live_listener(self):
        path, _ = self.endpoint()
        with patch.object(scheduled_runtime.socket, "socket"):
            with self.assertRaisesRegex(ValueError, "already_exists"):
                scheduled_runtime.clear_stale_socket(path)
        path.unlink.assert_not_called()

    def test_removes_only_stale_socket_with_same_inode(self):
        path, _ = self.endpoint()
        with patch.object(scheduled_runtime.socket, "socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError(errno.ECONNREFUSED, "synthetic")
            scheduled_runtime.clear_stale_socket(path)
        path.unlink.assert_called_once()

    def test_uncertain_socket_or_changed_inode_not_removed(self):
        for error in (errno.EACCES, errno.ETIMEDOUT):
            path, _ = self.endpoint()
            with patch.object(scheduled_runtime.socket, "socket") as factory:
                factory.return_value.__enter__.return_value.connect.side_effect = OSError(error, "synthetic")
                with self.assertRaisesRegex(ValueError, "unconfirmed"):
                    scheduled_runtime.clear_stale_socket(path)
            path.unlink.assert_not_called()
        path, before = self.endpoint()
        path.lstat.side_effect = [before, SimpleNamespace(st_dev=1, st_ino=3)]
        with patch.object(scheduled_runtime.socket, "socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError(errno.ECONNREFUSED, "synthetic")
            with self.assertRaisesRegex(ValueError, "changed"):
                scheduled_runtime.clear_stale_socket(path)
        path.unlink.assert_not_called()

    def test_adapter_reuses_store_sender_and_reaps_verifier(self):
        config = {"activation_date": "2026-11-04", "publisher_uid": os.getuid() + 1,
                  "socket_path": "/unused/scheduled.sock", "node_path": "/unused/node", "verifier_path": "/unused/verifier.mjs"}
        store, sender, relay_config = object(), object(), {}
        with patch.object(scheduled_runtime, "NodePublicationVerifier") as verifier, \
                patch.object(scheduled_runtime, "ScheduledDelivery") as queue:
            adapter = scheduled_runtime.ScheduledRuntime(config, store, relay_config, sender)
            queue.assert_called_once_with(store, relay_config, verifier.return_value, sender,
                                          activation_date="2026-11-04")
            adapter.close()
            verifier.return_value.close.assert_called_once()


class ScheduledLoopTests(unittest.TestCase):
    def controller(self):
        receiver = SimpleNamespace(store=object())
        scheduled = SimpleNamespace(queue=MagicMock(), assert_reconciled=MagicMock())
        return runtime.ControlledRelay(SimpleNamespace(), receiver, "a" * 40, scheduled=scheduled)

    def test_paused_never_ticks_and_unbound_unknown_pauses_all_senders(self):
        controller = self.controller()
        with patch.object(controller.scheduled, "assert_reconciled") as reconcile:
            controller.tick_scheduled()
            controller.scheduled.queue.tick.assert_not_called()
            controller.phase = "active"
            reconcile.side_effect = [None, ValueError("unknown")]
            with self.assertRaises(ValueError):
                controller.tick_scheduled()
        self.assertEqual(controller.phase, "paused")

    def test_due_ticks_continue_when_slack_read_retries(self):
        controller = self.controller()
        controller.phase = "active"
        server = MagicMock()
        count = 0
        clock = 1000.0

        def select_once(*args):
            nonlocal count, clock
            count += 1
            clock += 1
            return [], [], []

        with patch.object(runtime.select, "select", side_effect=select_once), \
                patch.object(runtime.time, "monotonic", side_effect=lambda: clock), \
                patch.object(runtime, "assert_reconciled"), \
                patch.object(controller, "poll", side_effect=runtime.RetryableReadError("history_read_retry")) as poll:
            runtime.run_loop(controller, server, {"poll_seconds": 60}, lambda: count >= 4)
        self.assertEqual(poll.call_count, 1)
        self.assertEqual(controller.scheduled.queue.tick.call_count, 3)
        self.assertEqual(controller.phase, "active")

    def test_control_pause_is_served_before_admission_and_tick(self):
        controller = self.controller()
        controller.phase = "active"
        control, admission = MagicMock(), MagicMock()
        events = []
        count = 0

        def select_once(*args):
            nonlocal count
            count += 1
            return [admission, control], [], []

        def pause():
            events.append("pause")
            controller.phase = "paused"

        control.handle_request.side_effect = pause
        admission.handle_request.side_effect = lambda: events.append("admit")
        with patch.object(runtime.select, "select", side_effect=select_once):
            runtime.run_loop(controller, control, {"poll_seconds": 60}, lambda: count >= 2, admission)
        self.assertEqual(events, ["pause", "admit"])
        controller.scheduled.queue.tick.assert_not_called()

    def test_default_runtime_does_not_load_or_create_scheduler(self):
        self.assertIsNone(runtime.ControlledRelay(None, None, "a" * 40).scheduled)

    def test_serve_uses_one_exclusive_lock_store_and_wecom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run").mkdir()
            module = MagicMock()
            module.exclusive.return_value = contextlib.nullcontext()
            receiver = MagicMock()
            receiver.wecom = module.WeCom.return_value
            with patch.object(runtime, "resume_class", return_value=MagicMock(return_value=receiver)), \
                    patch.object(runtime, "assert_reconciled"), \
                    patch.object(runtime.ControlledRelay, "ready"), \
                    patch.object(runtime.signal, "signal"), \
                    patch.object(runtime, "run_loop") as loop, \
                    patch.object(runtime.socketserver, "UnixStreamServer") as server, \
                    patch.object(scheduled_runtime, "load_config", return_value={"synthetic": True}), \
                    patch.object(scheduled_runtime, "ScheduledRuntime") as scheduler:
                server.side_effect = lambda *args: ((root / "run/relay.sock").touch() or MagicMock())
                module.Store.return_value.get.return_value = None
                runtime.serve(module, root, "a" * 40, scheduler_config=root / "scheduler.json")
            module.exclusive.assert_called_once_with(root)
            module.load_secrets.assert_called_once_with(root)
            module.WeCom.assert_called_once()
            module.Store.assert_called_once_with(root / "state")
            scheduler.assert_called_once_with({"synthetic": True}, module.Store.return_value,
                                              module.load_config.return_value, module.WeCom.return_value)
            loop.assert_called_once()
            self.assertIs(loop.call_args.args[-1], scheduler.return_value.open.return_value)
            self.assertFalse(scheduler.return_value.open.call_args.args[0]())
            scheduler.return_value.close.assert_called_once()
            module.Store.return_value.db.close.assert_called_once()


class FrozenScheduledGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = relay.Store(self.root)
        self.addCleanup(lambda: self.store.db.close())
        self.config = {"team_id": "TTEST", "channel_id": "DFIXED", "user_id": "UOWNER",
                       "sender_id": "UDOT", "poll_seconds": 60, "forward_images": True,
                       "notice_connector": {"user_id": "UOWNER", "app_id": "ACONNECTOR",
                                            "attribution_text": "*Send tool* <@UTOOL>",
                                            "report_base_url": "https://example.org/reports/",
                                            "not_before": "1700000000.000000"}}
        self.now = 1793491200.0
        self.calls = []
        self.plans = {}
        self.wecom = MagicMock()
        self.wecom.send.side_effect = lambda text: self.calls.append(text) or "unknown"
        self.wecom.send_image.return_value = "sent"
        self.adapter = self.new_adapter()
        self.controller = runtime.ControlledRelay(relay, SimpleNamespace(store=self.store), "a" * 40,
                                                  scheduled=self.adapter)
        self.controller.phase = "active"

    def new_adapter(self):
        # 真实队列验证持久化结构和绑定范围；仅源读取与 IPC 使用替身。
        adapter = scheduled_runtime.ScheduledRuntime.__new__(scheduled_runtime.ScheduledRuntime)
        adapter.queue = ScheduledDelivery(self.store, self.config,
                                          lambda bundle, commit: copy.deepcopy(self.plans[bundle]),
                                          self.wecom, lambda: self.now, activation_date="2026-11-01")
        return adapter

    def admit(self, day="2026-11-01"):
        request_id = "report-" + day
        bundle = ("synthetic source " + day).encode()
        poster = image_fixtures.PNG
        self.plans[bundle] = {"report_request_id": request_id, "report_date": day,
                              "report_commit": "a" * 40, "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
                              "notice_key": "https://example.org/reports/" + day + "/r1/",
                              "text": "Synthetic " + day, "poster": poster,
                              "poster_sha256": hashlib.sha256(poster).hexdigest(), "mime": "image/png"}
        self.adapter.queue.admit(request_id, bundle, "a" * 40)
        return request_id

    def freeze(self):
        request_id = self.admit()
        self.controller.tick_scheduled()
        self.assertEqual(self.adapter.queue.status(request_id)["phase"], "unknown")
        return request_id

    def test_frozen_unknown_does_not_block_independent_next_day_or_retry(self):
        first = self.freeze()
        self.admit("2026-11-02")
        self.now += 86400
        self.wecom.send.side_effect = lambda text: self.calls.append(text) or "sent"
        self.controller.tick_scheduled()
        self.now += 4
        self.controller.tick_scheduled()
        self.assertEqual(self.calls, ["Synthetic 2026-11-01", "Synthetic 2026-11-02"])
        self.assertEqual(self.adapter.queue.status(first)["phase"], "unknown")
        self.assertEqual(self.adapter.queue.status("report-2026-11-02")["status"], "sent")
        self.assertEqual(self.controller.phase, "active")

    def test_scheduled_only_preserves_frozen_unknown_and_next_day_progress(self):
        self.controller.automatic_delivery = "scheduled_only"
        self.test_frozen_unknown_does_not_block_independent_next_day_or_retry()
        self.assertEqual(self.controller.polls, 0)
        self.assertEqual(self.controller.scheduled_ticks, 3)

    def test_real_store_restart_preserves_unknown_and_scope(self):
        first = self.freeze()
        self.store.db.close()
        self.store = relay.Store(self.root)
        self.adapter = self.new_adapter()
        before = self.store.db.total_changes
        self.adapter.assert_reconciled()
        self.assertEqual(self.store.db.total_changes, before)
        self.now += 86400
        self.assertIsNone(self.adapter.queue.tick())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.adapter.queue.status(first)["status"], "unknown")

    def test_restart_after_sending_intent_does_not_replay(self):
        first = self.admit()
        with self.store.db:
            self.store.db.execute("UPDATE scheduled_notices SET phase='sending'")
            self.store.db.execute("UPDATE requests SET status='sending'")
            self.store.db.execute("UPDATE parts SET status='sending'")
        self.store.db.close()
        self.store = relay.Store(self.root)
        self.adapter = self.new_adapter()
        self.adapter.assert_reconciled()
        self.now += 86400
        self.assertIsNone(self.adapter.queue.tick())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.adapter.queue.status(first)["status"], "unknown")

    def test_disabled_scheduler_preserves_original_guard(self):
        self.freeze()
        with self.assertRaises(ValueError):
            assert_reconciled(self.store)
        controller = runtime.ControlledRelay(relay, SimpleNamespace(store=self.store), "a" * 40)
        with self.assertRaises(ValueError):
            controller.reconciled()

    def test_forged_mode_without_queue_binding_and_orphan_part_still_block(self):
        for table in ("requests", "parts", "image_parts"):
            with self.subTest(table=table):
                with self.store.db:
                    if table == "requests":
                        self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('forged','x','scheduled',1,'unknown')")
                    elif table == "parts":
                        self.store.db.execute("INSERT INTO parts VALUES ('forged',0,'x','unknown')")
                    else:
                        self.store.db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status) VALUES ('forged','poster',0,'unknown')")
                with self.assertRaises(ValueError):
                    self.adapter.assert_reconciled()
                with self.store.db:
                    self.store.db.execute("DELETE FROM " + table)

    def test_corrupted_binding_or_nonfrozen_phase_still_blocks(self):
        self.freeze()
        for query in ("UPDATE requests SET intent_hash='changed'",
                      "UPDATE scheduled_notices SET phase='pending'",
                      "DELETE FROM meta WHERE key LIKE 'notice_day:%'",
                      "UPDATE requests SET mode='auto'"):
            with self.subTest(query=query):
                self.store.db.execute("SAVEPOINT corruption")
                self.store.db.execute(query)
                with self.assertRaises(ValueError):
                    self.adapter.assert_reconciled()
                self.store.db.execute("ROLLBACK TO corruption")
                self.store.db.execute("RELEASE corruption")

    def test_frozen_scheduled_intents_do_not_authorize_deployment_cutover(self):
        self.freeze()
        result = self.controller.dispatch("/control/status", {"release": "a" * 40,
                                          "deadline": runtime.time.monotonic() + 30, "nonce": None}, os.getuid())
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["phase"], "active")

    def test_queued_and_retrying_work_blocks_cutover_until_complete(self):
        request_id = self.admit()
        for phase in ("pending", "verifying", "sending", "unknown"):
            with self.subTest(phase=phase):
                with self.store.db:
                    self.store.db.execute("UPDATE scheduled_notices SET phase=?", (phase,))
                with self.assertRaisesRegex(ValueError, "scheduled_delivery_blocks_cutover"):
                    assert_reconciled(self.store)
        with self.store.db:
            self.store.db.execute("UPDATE scheduled_notices SET phase='pending'")
        payload = {"release": "a" * 40, "deadline": runtime.time.monotonic() + 30, "nonce": None}
        self.assertFalse(self.controller.dispatch('/control/pause', payload, os.getuid())['reconciled'])
        # 暂停后状态仍可读取；同一串行控制器阻止暂停回执之后、停机之前的新准入。
        can_admit = lambda: self.controller.phase == 'active'
        before = self.store.db.total_changes
        result = scheduled_socket.dispatch(json.dumps({'action':'status','request_id':request_id}).encode(), self.adapter.queue, can_admit)
        self.assertEqual(result['status'], 'queued')
        with self.assertRaisesRegex(ValueError, 'admission paused'):
            scheduled_socket.dispatch(json.dumps({'action':'admit','request_id':'new-report','bundle_base64':'eA==','report_commit':'a'*40}).encode(), self.adapter.queue, can_admit)
        self.assertEqual(self.store.db.total_changes, before)
        self.controller.phase = 'active'
        self.wecom.send.side_effect = lambda text: 'sent'
        self.controller.tick_scheduled()
        self.now += 4
        self.controller.tick_scheduled()
        assert_reconciled(self.store)

    def test_authorized_revision_repeat_retains_history_without_poisoning_next_day(self):
        first = self.admit()
        self.wecom.send.side_effect = lambda text: 'sent'
        self.controller.tick_scheduled()
        self.now += 4
        self.controller.tick_scheduled()
        self.now += 4
        receiver = relay.Relay(self.store, None, self.wecom, self.config, clock=lambda: self.now)
        result = receiver.deliver('authorized-repeat-r2', {'mode':'manual'}, 'Synthetic revised notice',
                                  '1793491208.000001', 'manual', os.getuid(), repeat=True,
                                  reason='Explicit synthetic correction',
                                  notice_key='https://example.org/reports/2026-11-01/r2/')
        self.assertEqual(result['status'], 'sent')
        self.adapter = self.new_adapter()
        self.assertEqual(self.adapter.queue.status(first)['status'], 'sent')
        self.admit('2026-11-02')
        # 即使请求存在，使用另一天的日期声明仍须拒绝。
        day_key = 'notice_day:' + relay.digest(['TTEST','DFIXED','2026-11-01'])
        self.store.set(day_key, 'report-2026-11-02')
        with self.assertRaisesRegex(ValueError, 'daily claim conflict'):
            self.new_adapter()

    def test_manual_repeat_cannot_transfer_an_unfinished_scheduled_claim(self):
        first = self.admit()
        receiver = relay.Relay(self.store, None, self.wecom, self.config, clock=lambda: self.now)
        for frozen in (False, True):
            if frozen:
                self.controller.tick_scheduled()
                self.assertEqual(self.adapter.queue.status(first)['status'], 'unknown')
            before = self.store.db.total_changes
            calls = list(self.calls)
            for revision in (1, 2):
                with self.subTest(frozen=frozen, revision=revision), self.assertRaisesRegex(relay.RelayError, 'scheduled_reconciliation_required'):
                    receiver.deliver('blocked-repeat-'+str(revision), {}, 'Must not send',
                                     '1793491208.000001', 'manual', os.getuid(), repeat=True,
                                     reason='Synthetic explicit correction',
                                     notice_key=f'https://example.org/reports/2026-11-01/r{revision}/')
            self.assertEqual(self.store.db.total_changes, before)
            self.assertEqual(self.calls, calls)
            self.adapter.assert_reconciled()
            self.assertEqual(self.controller.phase, 'active')


if __name__ == "__main__":
    unittest.main()
