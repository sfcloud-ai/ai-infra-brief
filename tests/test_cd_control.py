"""受限入口、单实例交接与回滚回归；服务故障均为构造注入。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch

from deploy.policy import Policy, PolicyError
from deploy.release import ReleaseError, Releases, write_json
from deploy.ssh_entry import parse_command
from deploy.stage_helper import execute
from deploy.supervisor import DockerDriver, ServiceError
from deploy import transport
from deploy import release as release_module
from integrations.slack_wecom.runtime import ControlledRelay, RetryableReadError
from test_relay_integration import relay, relay_fixtures
from deploy.relay_resume import binding, resume_class


class FakeDriver:
    def __init__(self):
        self.legacy = "f" * 40
        self.running = self.legacy
        self.phase = "active"
        self.events = []
        self.fail_ready_once = None
        self.refuse_drain = None
        self.missing = set()
        self.ledger = {"cursor": "synthetic-cursor", "dedup": ["already-sent"]}

    def capture_legacy(self):
        self.events.append(("capture", self.legacy))
        return self.legacy

    def preflight(self, release):
        if release in self.missing:
            raise ServiceError("release_not_retained_or_ready")

    def current_sender(self):
        return self.running

    def quiesce(self, release):
        if release == self.refuse_drain:
            raise ServiceError("constructed_unconfirmed_drain")
        if self.running != release:
            raise ServiceError("unexpected_sender")
        self.events.append(("stop", release))
        self.running = None

    def handoff(self, release):
        if self.running:
            raise ServiceError("handoff_with_sender_running")
        self.events.append(("handoff", release))

    def abort_start(self, release, before):
        if self.running == release:
            if self.phase != "paused":
                raise ServiceError("candidate_not_paused")
            self.events.append(("abort_start", release))
            self.running = None

    def start_paused(self, release):
        if self.running:
            raise ServiceError("parallel_sender")
        self.events.append(("start_paused", release))
        self.running, self.phase = release, "paused"

    def wait_ready(self, release):
        if self.fail_ready_once == release:
            self.fail_ready_once = None
            raise ServiceError("constructed_health_failure")
        if self.running != release or self.phase != "paused":
            raise ServiceError("not_paused_ready")

    def activate(self, release):
        self.wait_ready(release)
        self.events.append(("activate", release))
        self.phase = "active"

    def wait_healthy(self, release):
        if self.running != release or self.phase != "active":
            raise ServiceError("unhealthy")


class RestrictedHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = FakeDriver()
        self.staged = []

    def invoke(self, command):
        return execute(parse_command(command), {"live_enabled": True}, self.root,
                       stage_runner=lambda config, commit, checksum, protected: self.staged.append(commit),
                       driver_factory=lambda *_: self.driver)

    def deploy(self, sequence, commit):
        return self.invoke("deploy " + str(sequence) + " " + commit + " " + "d" * 64)

    def test_deploy_and_rollback_use_same_restricted_dispatch_and_preserve_ledger(self):
        first, second = "a" * 40, "b" * 40
        original = json.dumps(self.driver.ledger, sort_keys=True)
        self.deploy(10, first)
        self.deploy(11, second)
        status = self.invoke("status")
        self.assertEqual(status["active"], second)
        result = self.invoke("rollback 11 " + second + " " + first)
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.driver.running, first)
        self.assertEqual(json.dumps(self.driver.ledger, sort_keys=True), original)
        self.assertEqual(Policy(self.root).value["sequence"], 11)

    def test_stale_event_and_replay_after_explicit_rollback_are_rejected_before_stage(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.deploy(11, second)
        self.invoke("rollback 11 " + second + " " + first)
        count = len(self.staged)
        for sequence, commit in ((10, first), (11, second)):
            with self.assertRaises(PolicyError):
                self.deploy(sequence, commit)
        self.assertEqual(len(self.staged), count)
        self.deploy(12, "c" * 40)
        self.assertEqual(self.driver.running, "c" * 40)

    def test_rollback_compare_and_history_reject_races_and_unknown_target(self):
        self.deploy(10, "a" * 40)
        before = list(self.driver.events)
        for command in ("rollback 9 " + "a" * 40 + " " + self.driver.legacy,
                        "rollback 10 " + "b" * 40 + " " + self.driver.legacy,
                        "rollback 10 " + "a" * 40 + " " + "c" * 40):
            with self.assertRaises(PolicyError):
                self.invoke(command)
        self.assertEqual(before, self.driver.events)

    def test_pruned_rollback_target_is_rejected_before_stopping_current(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.deploy(11, second)
        self.driver.missing.add(first)
        before = list(self.driver.events)
        with self.assertRaisesRegex(ServiceError, "release_not_retained"):
            self.invoke("rollback 11 " + second + " " + first)
        self.assertEqual(self.driver.events, before)
        self.assertEqual(self.driver.running, second)

    def test_crash_after_rollback_switch_still_blocks_original_event_replay(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.deploy(11, second)
        with patch.object(Policy, "rolled_back", side_effect=RuntimeError("constructed_crash")):
            with self.assertRaises(RuntimeError):
                self.invoke("rollback 11 " + second + " " + first)
        self.assertEqual(self.driver.running, first)
        with self.assertRaises(PolicyError):
            self.deploy(11, second)

    def test_pending_recovery_precedes_new_event_staging_and_protects_both_versions(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        write_json(self.root / "switch-pending.json", {"before": first, "target": second})
        self.driver.running = second
        self.driver.refuse_drain = second
        before = list(self.staged)
        for sequence in (11, 12, 13):
            with self.assertRaises(ServiceError):
                self.deploy(sequence, "c" * 40)
        self.assertEqual(self.staged, before)
        self.assertEqual(Policy(self.root).value["sequence"], 10)
        self.assertTrue({first, second} <= Policy(self.root).protected())

    def test_drain_revokes_restart_activation_before_pause_ack(self):
        driver = DockerDriver.__new__(DockerDriver)
        driver.activation = self.root
        driver.live = {"container": "synthetic-managed"}
        write_json(self.root / "active.json", {"release": "a" * 40})

        def status(release, operation):
            self.assertEqual(json.loads((self.root / "active.json").read_text()), {"release": None})
            self.assertEqual(json.loads((self.root / "permit.json").read_text()), {"release": None, "nonce": None})
            return {"phase": "paused", "reconciled": True, "in_flight": False}

        with patch.object(driver, "running_release", side_effect=["a" * 40, None]), \
                patch.object(driver, "status", side_effect=status), patch.object(driver, "command"):
            driver.quiesce("a" * 40)

    def test_failed_new_health_rolls_back_without_parallel_sender_or_state_rewind(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.driver.fail_ready_once = second
        with self.assertRaises(ServiceError):
            self.deploy(11, second)
        self.assertEqual(self.driver.running, first)
        self.assertEqual(Policy(self.root).value["active"], first)
        self.assertFalse((self.root / "switch-pending.json").exists())
        self.assertEqual(self.driver.ledger["dedup"], ["already-sent"])
        self.deploy(11, second)
        self.assertEqual(self.driver.running, second)

    def test_unconfirmed_drain_keeps_pending_and_never_starts_second_sender(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.driver.refuse_drain = first
        before = len([event for event in self.driver.events if event[0] == "start_paused"])
        with self.assertRaises(ServiceError):
            self.deploy(11, second)
        self.assertEqual(self.driver.running, first)
        self.assertTrue((self.root / "switch-pending.json").exists())
        self.assertEqual(before, len([event for event in self.driver.events if event[0] == "start_paused"]))

    def test_public_pr_trigger_is_rejected_before_file_or_secret_access(self):
        environment = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_REPOSITORY": "sfcloud-ai/ai-infra-brief"}
        with patch.dict(os.environ, environment, clear=True), patch("sys.argv", ["transport.py", "missing"]):
            with self.assertRaisesRegex(ValueError, "untrusted_trigger"):
                transport.main()


class RetentionTests(unittest.TestCase):
    def test_low_space_is_rejected_before_reading_or_writing_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            incoming = Mock()
            arguments = ["release.py", "--root", directory, "--python", "/python",
                         "deploy", "a" * 40, "d" * 64]
            with patch("sys.argv", arguments), patch("sys.stdin", incoming), \
                    patch("deploy.release.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(ReleaseError, "temporary_free_space_limit"):
                    release_module.main()
            incoming.buffer.read.assert_not_called()
            self.assertEqual(list((Path(directory) / "incoming").iterdir()), [])

    def test_abandoned_temporary_cleanup_rejects_symlink_and_preserves_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Releases(directory, "/python", reserve=0)
            abandoned = Path(directory) / "incoming/upload-abcdefgh"
            abandoned.mkdir()
            (abandoned / "partial").write_text("synthetic")
            manager.cleanup_abandoned()
            self.assertFalse(abandoned.exists())
            outside = Path(directory) / "external"
            outside.mkdir()
            (outside / "keep").write_text("synthetic")
            abandoned.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ReleaseError, "unexpected_temporary_path"):
                manager.cleanup_abandoned()
            self.assertTrue((outside / "keep").exists())

    def test_retention_keeps_live_previous_and_requested_release(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Releases(directory, "/python", protected={"2" * 40}, keep=5, reserve=0)
            for digit in "12345":
                path = Path(directory) / "releases" / (digit * 40)
                path.mkdir()
                write_json(path / "ready.json", {"commit": digit * 40})
                os.utime(path, (int(digit), int(digit)))
            manager.point("current", "4" * 40)
            manager.point("previous", "3" * 40)
            manager.retain("6" * 40, 1024)
            self.assertFalse((Path(directory) / "releases" / ("1" * 40)).exists())
            for digit in "2345":
                self.assertTrue((Path(directory) / "releases" / (digit * 40)).exists())

    def test_disk_reserve_rejects_without_removing_protected_release(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Releases(directory, "/python", protected={"a" * 40}, reserve=1024)
            path = Path(directory) / "releases" / ("a" * 40)
            path.mkdir()
            with patch("deploy.release.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(ReleaseError, "free_space"):
                    manager.retain("b" * 40, 100)
            self.assertTrue(path.exists())


class RuntimeControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = relay.Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.store.set("cursor", relay_fixtures.ts(1))
        self.store.set("handoff_binding_v1", binding(relay_fixtures.CONFIG))
        self.slack, self.wecom = relay_fixtures.FakeSlack(), relay_fixtures.FakeWeCom()
        self.receiver = resume_class(relay)(self.store, self.slack, self.wecom, relay_fixtures.CONFIG)
        self.receiver.baseline()
        self.controller = ControlledRelay(relay, self.receiver, "a" * 40)

    def command(self, operation):
        return self.controller.dispatch("/control/" + operation,
                                        {"release": "a" * 40, "deadline": time.monotonic() + 180, "nonce": None},
                                        os.getuid())

    def test_paused_start_blocks_automatic_and_manual_delivery_until_activation(self):
        self.slack.messages = [relay_fixtures.brief(2)]
        self.controller.poll()
        with self.assertRaises(relay.RelayError):
            self.controller.dispatch("/manual/text", {"request_id": "synthetic", "text": "test"}, os.getuid())
        self.assertEqual(self.wecom.sent, [])
        self.command("activate")
        self.assertEqual(self.wecom.sent, [])
        self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        result = self.command("pause")
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["successful_polls"], 1)
        self.slack.messages.append(relay_fixtures.brief(3))
        self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(2))

    def test_unreadable_history_fails_activation_without_advancing_cursor(self):
        self.slack.fail = True
        with self.assertRaises(relay.RelayError):
            self.command("activate")
        self.assertEqual(self.controller.phase, "paused")
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))
        self.assertEqual(self.wecom.sent, [])

    def test_unknown_receipt_pauses_and_blocks_healthy_status(self):
        self.slack.messages = [relay_fixtures.brief(2)]
        self.wecom.results = ["unknown"]
        self.command("activate")
        with self.assertRaises(ValueError):
            self.controller.poll()
        result = self.command("status")
        self.assertEqual(result["phase"], "paused")
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["successful_polls"], 0)

    def test_transient_read_recovers_without_reset_or_duplicate(self):
        self.command("activate")
        self.slack.messages = [relay_fixtures.brief(2)]
        with patch.object(self.slack, "newer", side_effect=relay.RelayError("slack_request_failed", 502)):
            with self.assertRaises(RetryableReadError):
                self.controller.poll()
        self.assertEqual(self.controller.phase, "active")
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))
        self.assertEqual(self.wecom.sent, [])
        self.controller.poll()
        with patch.object(self.slack, "newer", side_effect=relay.RelayError("slack_api_rejected", 502)):
            with self.assertRaises(RetryableReadError):
                self.controller.poll()
        self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(2))
        self.assertNotIn("latest", self.slack.calls)

    def test_manual_unknown_immediately_blocks_following_delivery(self):
        self.command("activate")
        self.wecom.results = ["unknown"]
        result = self.controller.dispatch("/manual/text", {"request_id": "synthetic-manual", "text": "test"}, os.getuid())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.controller.phase, "paused")
        self.slack.messages = [relay_fixtures.brief(2)]
        self.controller.poll()
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))

    def test_driver_deadline_precedes_exec_and_timeout_leaves_old_sender(self):
        driver = DockerDriver.__new__(DockerDriver)
        driver.live = {"container": "synthetic"}
        driver.activation = Path(self.temp.name)
        clock = [100.0]
        def running():
            clock[0] += 12
            return "a" * 40
        def command(arguments, timeout):
            self.assertEqual(float(arguments[arguments.index("--deadline") + 1]), 1900.0)
            self.assertEqual(timeout, 1803.0)
            raise subprocess.TimeoutExpired("synthetic_exec", timeout)
        with patch("deploy.supervisor.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(driver, "running_release", side_effect=running), \
                patch.object(driver, "code", return_value="/synthetic"), \
                patch.object(driver, "command", side_effect=command):
            with self.assertRaises(subprocess.TimeoutExpired):
                driver.status("a" * 40, "pause")
        with patch.object(driver, "running_release", return_value="a" * 40), \
                patch.object(driver, "status", side_effect=subprocess.TimeoutExpired("synthetic_exec", 1800)), \
                patch.object(driver, "command") as docker:
            with self.assertRaises(subprocess.TimeoutExpired):
                driver.quiesce("a" * 40)
        docker.assert_not_called()
        self.assertEqual(json.loads((driver.activation / "permit.json").read_text()), {"release": None, "nonce": None})

    def test_health_retry_uses_one_deadline(self):
        driver = DockerDriver.__new__(DockerDriver)
        clock, deadlines = [100.0], []
        def status(release, deadline):
            deadlines.append(deadline)
            clock[0] += 1000
            raise subprocess.TimeoutExpired("synthetic_exec", 1)
        with patch("deploy.supervisor.time.monotonic", side_effect=lambda: clock[0]), \
                patch("deploy.supervisor.time.sleep"), patch.object(driver, "status", side_effect=status):
            with self.assertRaisesRegex(ServiceError, "relay_health_deadline"):
                driver.wait("a" * 40, active=True)
        self.assertEqual(deadlines, [1900.0, 1900.0])

    def test_cached_batch_yields_before_second_message_and_respects_pause(self):
        self.command("activate")
        self.slack.messages = [relay_fixtures.brief(2), relay_fixtures.brief(3)]
        self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(len(self.receiver.pending_messages), 1)
        self.command("pause")
        self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.command("activate")
        with patch.object(self.slack, "newer", side_effect=AssertionError("batch_must_not_be_refetched")):
            self.controller.poll()
        self.assertEqual(len(self.wecom.sent), 2)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(3))

    def test_pause_after_read_failure_is_not_automatically_reactivated(self):
        self.command("activate")
        with patch.object(self.slack, "newer", side_effect=relay.RelayError("slack_request_failed", 502)):
            with self.assertRaises(RetryableReadError):
                self.controller.poll()
        self.command("pause")
        self.slack.messages = [relay_fixtures.brief(2)]
        self.controller.poll()
        self.assertEqual(self.controller.phase, "paused")
        self.assertEqual(self.wecom.sent, [])

    def test_nonread_fault_and_existing_unknown_remain_blocking(self):
        self.command("activate")
        with patch.object(self.slack, "newer", side_effect=relay.RelayError("slack_backlog_skipped", 409)):
            with self.assertRaises(relay.RelayError):
                self.controller.poll()
        self.assertEqual(self.controller.phase, "paused")
        self.command("activate")
        self.slack.messages = [relay_fixtures.brief(2)]
        with patch.object(self.store, "set", side_effect=sqlite3.OperationalError("constructed_write_failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.controller.poll()
        self.assertEqual(self.controller.phase, "paused")
        self.assertEqual(len(self.wecom.sent), 0)
        with self.assertRaises(ValueError):
            self.command("activate")

    def test_existing_unknown_blocks_before_history_read(self):
        self.command("activate")
        self.store.db.execute("INSERT INTO parts VALUES ('synthetic',0,'hash','unknown')")
        self.store.db.commit()
        with patch.object(self.slack, "newer") as read:
            with self.assertRaises(ValueError):
                self.controller.poll()
        read.assert_not_called()
        self.assertEqual(self.controller.phase, "paused")

    def test_expired_control_never_changes_phase(self):
        self.command("activate")
        for operation in ("activate", "pause"):
            payload = {"release": "a" * 40, "deadline": time.monotonic() - 1, "nonce": None}
            with self.assertRaisesRegex(relay.RelayError, "control_request_expired"):
                self.controller.dispatch("/control/" + operation, payload, os.getuid())
            self.assertEqual(self.controller.phase, "active")

    def test_activation_expiring_during_readiness_stays_paused(self):
        payload = {"release": "a" * 40, "deadline": 101.0, "nonce": None}
        clock = [100.0]
        def ready():
            clock[0] = 102.0
        with patch("integrations.slack_wecom.runtime.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.controller, "ready", side_effect=ready):
            with self.assertRaisesRegex(relay.RelayError, "control_request_expired"):
                self.controller.dispatch("/control/activate", payload, os.getuid())
        self.assertEqual(self.controller.phase, "paused")

    def test_revoked_activation_cannot_arrive_after_pause_or_readiness(self):
        self.controller.activation_file = Path(self.temp.name) / "active.json"
        nonce = "e" * 32
        permit = {"release": "a" * 40, "nonce": nonce}
        payload = {"release": "a" * 40, "deadline": time.monotonic() + 180, "nonce": nonce}
        with patch.object(self.controller, "permit", side_effect=lambda: dict(permit)):
            self.controller.dispatch("/control/activate", payload, os.getuid())
            # 模拟 helper 退出后恢复程序先撤销授权并确认暂停，旧请求随后到达。
            permit.update(release=None, nonce=None)
            self.command("pause")
            with self.assertRaisesRegex(relay.RelayError, "activation_permit_revoked"):
                self.controller.dispatch("/control/activate", payload, os.getuid())
            self.assertEqual(self.controller.phase, "paused")
            permit.update(release="a" * 40, nonce=nonce)
            with patch.object(self.controller, "ready", side_effect=lambda: permit.update(release=None, nonce=None)):
                with self.assertRaisesRegex(relay.RelayError, "activation_permit_revoked"):
                    self.controller.dispatch("/control/activate", payload, os.getuid())
        self.assertEqual(self.controller.phase, "paused")
        self.assertEqual(self.wecom.sent, [])
