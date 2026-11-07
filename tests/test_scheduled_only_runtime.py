"""显式单路自动投递的离线回归；所有外部传输与服务监听均为替身。"""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from integrations.slack_wecom import relay, runtime, scheduled_runtime
from integrations.slack_wecom.resume import assert_reconciled, binding, resume_class
from deploy.supervisor import DockerDriver, ServiceError
from test_relay_integration import relay_fixtures


OPTIONS = {"activation_date": "2026-11-08", "automatic_delivery": "scheduled_only"}


class ScheduledOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = relay.Store(self.root)
        self.addCleanup(self.store.db.close)
        self.config = relay_fixtures.CONFIG
        self.store.set("cursor", relay_fixtures.ts(1))
        self.store.set("handoff_binding_v1", binding(self.config))
        self.slack, self.wecom = relay_fixtures.FakeSlack(), relay_fixtures.FakeWeCom()
        self.receiver = resume_class(relay)(self.store, self.slack, self.wecom, self.config)
        self.receiver.baseline()
        self.scheduled = SimpleNamespace(queue=MagicMock(), assert_reconciled=lambda: assert_reconciled(self.store))
        self.controller = runtime.ControlledRelay(relay, self.receiver, "a" * 40,
                                                 scheduled=self.scheduled, automatic_delivery="scheduled_only")

    def select(self, options=OPTIONS, bind_mode=True):
        return runtime.automatic_delivery_policy(self.store, self.config, options, bind_mode=bind_mode)

    def command(self, operation):
        return self.controller.dispatch("/control/" + operation,
            {"release": "a" * 40, "deadline": time.monotonic() + 180, "nonce": None}, os.getuid())

    def test_mode_selection_is_not_a_cursor_reset_or_new_identity(self):
        before = self.store.get("cursor"), self.store.get("handoff_binding_v1")
        self.assertEqual(self.select(bind_mode=False), "scheduled_only")
        self.assertIsNone(self.store.get("automatic_delivery_scope_v1"))
        self.select()
        changes = self.store.db.total_changes
        self.select()
        self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(before, (self.store.get("cursor"), self.store.get("handoff_binding_v1")))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_selected_mode_cannot_silently_fall_back_or_change_scope(self):
        self.select()
        for options in (None, {"activation_date": OPTIONS["activation_date"]}, {**OPTIONS, "activation_date": "2026-11-09"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.select(options)
        with self.assertRaisesRegex(ValueError, "scope_changed"):
            runtime.automatic_delivery_policy(self.store, {**self.config, "channel_id": "DOTHER"}, OPTIONS)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))

    def test_legacy_config_remains_compatible_without_writing_mode(self):
        before = self.store.db.total_changes
        self.assertEqual(self.select(None), "legacy")
        self.assertEqual(self.select({"activation_date": OPTIONS["activation_date"]}), "legacy")
        self.assertEqual(self.store.db.total_changes, before)

    def test_first_selection_rejects_native_unknown_without_changing_it(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('old','hash','auto',1,'unknown')")
            self.store.db.execute("INSERT INTO parts VALUES ('old',0,'hash','unknown')")
        before = list(self.store.db.iterdump())
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            self.select()
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertIsNone(self.store.get("automatic_delivery_scope_v1"))
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            self.command("activate")
        self.assertEqual(self.command("status")["phase"], "paused")
        self.assertEqual(self.controller.dispatch("/status", {"request_id": "old"}, os.getuid())["status"], "unknown")

    def test_first_selection_requires_pending_scheduled_jobs_to_drain(self):
        self.store.db.execute("CREATE TABLE scheduled_notices (phase TEXT)")
        self.store.db.execute("INSERT INTO scheduled_notices VALUES ('pending')")
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "unfinished_scheduled"):
            self.select()
        self.assertIsNone(self.store.get("automatic_delivery_scope_v1"))
        self.assertEqual(self.store.db.execute("SELECT phase FROM scheduled_notices").fetchone()[0], "pending")

    def test_mode_survives_real_store_restart_without_replaying_backlog(self):
        self.select()
        self.store.db.close()
        self.store = relay.Store(self.root)
        self.select()
        with self.assertRaisesRegex(ValueError, "requires_reconciliation"):
            self.select(None)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))

    def test_active_mode_never_polls_legacy_even_with_backlog_or_slack_failure(self):
        self.select()
        self.slack.messages = [relay_fixtures.brief(2)]
        self.slack.fail = True
        self.command("activate")
        with patch.object(self.receiver, "poll", side_effect=AssertionError("legacy auto must stay disabled")) as poll:
            self.controller.poll()
            count, clock = 0, 1000.0
            def select_once(*args):
                nonlocal count, clock
                count += 1
                clock += 1
                return [], [], []
            with patch.object(runtime.select, "select", side_effect=select_once), \
                    patch.object(runtime.time, "monotonic", side_effect=lambda: clock):
                runtime.run_loop(self.controller, MagicMock(), self.config, lambda: count >= 4)
            poll.assert_not_called()
        self.assertEqual(self.controller.scheduled_ticks, 3)
        self.assertEqual(self.controller.polls, 0)
        self.assertEqual(self.wecom.sent, [])
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))
        self.assertEqual(self.slack.calls, [])
        status = self.command("pause")
        self.assertEqual(status["phase"], "paused")
        self.assertEqual(status["automatic_delivery"], "scheduled_only")
        self.controller.tick_scheduled()
        self.assertEqual(self.controller.scheduled_ticks, 3)

    def test_mode_requires_scheduler_and_retains_native_unknown_guard(self):
        with self.assertRaisesRegex(ValueError, "runtime_required"):
            runtime.ControlledRelay(relay, self.receiver, "a" * 40, automatic_delivery="scheduled_only")
        self.select()
        self.command("activate")
        with self.store.db:
            self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('old','hash','auto',1,'unknown')")
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            self.controller.tick_scheduled()
        self.scheduled.queue.tick.assert_not_called()
        self.assertEqual(self.controller.phase, "paused")

    def test_explicit_manual_slack_read_still_verifies_identity_before_access(self):
        self.select(); self.command("activate")
        message = relay_fixtures.brief(2)
        payload = {"request_id": "manual-first", "message_ts": message["ts"]}
        events = []
        def exact(*args):
            events.append("exact")
            return message
        with patch.object(self.slack, "verify", create=True, side_effect=lambda: events.append("verify")), \
                patch.object(self.slack, "exact", side_effect=exact):
            result = self.controller.dispatch("/manual/slack-message", payload, os.getuid())
        self.assertEqual(result["status"], "sent")
        self.assertEqual(events, ["verify", "exact"])

    def test_saved_manual_receipt_and_conflict_need_no_slack_or_second_send(self):
        self.select(); self.command("activate")
        self.slack.messages = [relay_fixtures.brief(2)]
        payload = {"request_id": "manual-receipt", "message_ts": relay_fixtures.ts(2)}
        with patch.object(self.slack, "verify", create=True):
            original = self.controller.dispatch("/manual/slack-message", payload, os.getuid())
        before = list(self.store.db.iterdump())
        with patch.object(self.slack, "verify", create=True, side_effect=relay.RelayError("slack_request_failed")) as verify, \
                patch.object(self.slack, "exact", side_effect=AssertionError("must not read")) as exact:
            self.assertEqual(self.controller.dispatch("/manual/slack-message", payload, os.getuid()), original)
            with self.assertRaisesRegex(relay.RelayError, "request_id_conflict"):
                self.controller.dispatch("/manual/slack-message", {**payload, "reason": "different intent"}, os.getuid())
            with self.assertRaisesRegex(relay.RelayError, "forbidden"):
                self.controller.dispatch("/manual/slack-message", payload, os.getuid() + 1)
            verify.assert_not_called(); exact.assert_not_called()
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(len(self.wecom.sent), 1)

    def test_known_failed_manual_receipt_is_read_without_reauthentication(self):
        self.select(); self.command("activate")
        self.slack.messages = [relay_fixtures.brief(2)]
        self.wecom.results = ["failed"]
        payload = {"request_id": "manual-failed", "message_ts": relay_fixtures.ts(2)}
        with patch.object(self.slack, "verify", create=True):
            original = self.controller.dispatch("/manual/slack-message", payload, os.getuid())
        self.assertEqual(original["status"], "failed")
        before = list(self.store.db.iterdump())
        with patch.object(self.slack, "verify", create=True, side_effect=AssertionError("must not verify")), \
                patch.object(self.slack, "exact", side_effect=AssertionError("must not read")):
            self.assertEqual(self.controller.dispatch("/manual/slack-message", payload, os.getuid()), original)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(len(self.wecom.sent), 1)

    def test_new_manual_request_still_rejects_bad_identity_before_read_or_write(self):
        self.select(); self.command("activate")
        before = list(self.store.db.iterdump())
        with patch.object(self.slack, "verify", create=True, side_effect=relay.RelayError("wrong_slack_identity", 403)) as verify, \
                patch.object(self.slack, "exact") as exact:
            with self.assertRaisesRegex(relay.RelayError, "invalid_request_id"):
                self.controller.dispatch("/manual/slack-message", {}, os.getuid())
            verify.assert_not_called()
            with self.assertRaisesRegex(relay.RelayError, "wrong_slack_identity"):
                self.controller.dispatch("/manual/slack-message", {"request_id": "manual-first", "message_ts": relay_fixtures.ts(2)}, os.getuid())
            verify.assert_called_once(); exact.assert_not_called()
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(self.wecom.sent, [])


class ScheduledOnlyStartupTests(unittest.TestCase):
    def test_serve_skips_slack_network_but_checks_bound_receiver_and_pins_after_readiness(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "run").mkdir()
            module = MagicMock()
            module.load_config.return_value = relay_fixtures.CONFIG
            module.load_secrets.return_value = {"slack_user_token": "synthetic", "wecom_webhook": "synthetic"}
            module.Store.return_value.get.return_value = None
            events = []
            with patch.object(runtime, "resume_class") as receiver, \
                    patch.object(runtime.ControlledRelay, "ready", side_effect=lambda: events.append("ready")), \
                    patch.object(runtime, "assert_reconciled", side_effect=lambda store: events.append("bind")), \
                    patch.object(runtime.signal, "signal"), patch.object(runtime, "run_loop") as loop, \
                    patch.object(runtime.socketserver, "UnixStreamServer") as server, \
                    patch.object(scheduled_runtime, "load_config", return_value=OPTIONS), \
                    patch.object(scheduled_runtime, "ScheduledRuntime"):
                server.side_effect = lambda *args: ((root / "run/relay.sock").touch() or MagicMock())
                runtime.serve(module, root, "a" * 40, scheduler_config=root / "scheduler.json")
            module.Slack.return_value.verify.assert_not_called()
            receiver.return_value.return_value.baseline.assert_called_once()
            self.assertEqual(events, ["ready", "bind"])
            self.assertEqual(loop.call_args.args[0].automatic_delivery, "scheduled_only")


class ScheduledOnlyHealthTests(unittest.TestCase):
    def driver(self, mode="scheduled_only"):
        driver = DockerDriver.__new__(DockerDriver)
        driver.scheduler = SimpleNamespace(value={"automatic_delivery": mode})
        driver.assert_scheduler_binding = MagicMock()
        driver.status = MagicMock()
        return driver

    def test_health_uses_scheduled_ticks_not_legacy_polls(self):
        driver = self.driver()
        driver.status.return_value = {"phase": "active", "delivery_ready": True, "in_flight": False,
            "automatic_delivery": "scheduled_only", "successful_polls": 0, "successful_scheduled_ticks": 1}
        driver.wait("a" * 40, active=True)
        driver.status.assert_called_once()

    def test_wrong_mode_or_missing_tick_does_not_claim_healthy(self):
        for changed in ({"automatic_delivery": "legacy"}, {"successful_scheduled_ticks": 0}):
            driver = self.driver()
            driver.status.return_value = {"phase": "active", "delivery_ready": True, "in_flight": False,
                "automatic_delivery": "scheduled_only", "successful_polls": 99, "successful_scheduled_ticks": 1, **changed}
            with self.subTest(changed=changed), patch("deploy.supervisor.time.monotonic", side_effect=[0, 1, 1801]), \
                    patch("deploy.supervisor.time.sleep"), self.assertRaisesRegex(ServiceError, "health_deadline"):
                driver.wait("a" * 40, active=True)
