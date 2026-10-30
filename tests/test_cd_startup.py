"""候选未建立控制 socket 时的恢复，以及激活不确定时的保守阻断。"""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from deploy.release import write_json
from deploy.supervisor import DockerDriver, ServiceError
import test_cd_control as fixtures


class StartupRecoveryTests(unittest.TestCase):
    setUp = fixtures.RestrictedHelperTests.setUp
    invoke = fixtures.RestrictedHelperTests.invoke
    deploy = fixtures.RestrictedHelperTests.deploy

    def test_start_failure_without_socket_restores_previous_after_handoff(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.driver.fail_ready_once = second
        self.driver.refuse_drain = second
        with self.assertRaisesRegex(ServiceError, "constructed_health_failure"):
            self.deploy(11, second)
        abort = self.driver.events.index(("abort_start", second))
        self.assertEqual(self.driver.events[abort:abort + 4], [
            ("abort_start", second), ("handoff", first), ("start_paused", first), ("activate", first)])
        self.assertEqual(self.driver.running, first)
        self.assertEqual(self.driver.ledger, {"cursor": "synthetic-cursor", "dedup": ["already-sent"]})

    def test_activation_marker_precedes_request_and_prevents_forced_stop(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)

        def fail_activation(release):
            pending = json.loads((self.root / "switch-pending.json").read_text())
            self.assertIs(pending["activation_attempted"], True)
            self.driver.refuse_drain = release
            raise ServiceError("constructed_lost_activation_response")

        with patch.object(self.driver, "activate", side_effect=fail_activation):
            with self.assertRaises(ServiceError):
                self.deploy(11, second)
        self.assertEqual(self.driver.running, second)
        self.assertNotIn(("abort_start", second), self.driver.events)
        self.assertTrue((self.root / "switch-pending.json").exists())

    def test_unreconciled_ledger_blocks_restart_after_candidate_termination(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.driver.fail_ready_once = second
        original = self.driver.handoff

        def handoff(release):
            if release == first:
                raise ServiceError("unreconciled_delivery_blocks_cutover")
            return original(release)

        with patch.object(self.driver, "handoff", side_effect=handoff):
            with self.assertRaisesRegex(ServiceError, "unreconciled_delivery"):
                self.deploy(11, second)
        self.assertIsNone(self.driver.running)
        self.assertTrue((self.root / "switch-pending.json").exists())

    def test_interruption_after_candidate_stop_keeps_pending_and_recovers_once(self):
        first, second = "a" * 40, "b" * 40
        self.deploy(10, first)
        self.driver.fail_ready_once = second
        original = self.driver.abort_start

        def interrupted(release, before):
            original(release, before)
            raise ServiceError("constructed_policy_update_failure")

        with patch.object(self.driver, "abort_start", side_effect=interrupted):
            with self.assertRaisesRegex(ServiceError, "policy_update_failure"):
                self.deploy(11, second)
        self.assertIsNone(self.driver.running)
        self.assertTrue((self.root / "switch-pending.json").exists())
        with self.assertRaisesRegex(ServiceError, "interrupted_switch_recovered"):
            self.deploy(12, "c" * 40)
        self.assertEqual(self.driver.running, first)
        self.assertFalse((self.root / "switch-pending.json").exists())
        self.assertNotIn("c" * 40, self.staged)


class CandidateTerminationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = DockerDriver.__new__(DockerDriver)
        self.driver.activation = self.root
        self.driver.live = {"container": "synthetic-managed"}
        self.release, self.before, self.identity = "a" * 40, "b" * 40, "c" * 64
        self.current = {"Config": {"Labels": {"sfcloud.ai.service": "ai-infra-brief",
                                            "sfcloud.ai.release": self.release}},
                        "State": {"Running": False, "Restarting": True, "Paused": False}}
        self.commands = []
        self.absent, self.stop_sticks, self.fail = False, True, None

    def command(self, arguments, **kwargs):
        self.commands.append(arguments)
        if arguments[0] == self.fail:
            raise ServiceError("constructed_docker_failure")
        if arguments[0] == "container":
            return SimpleNamespace(stdout=b"" if self.absent else self.identity.encode())
        if arguments[0] == "inspect":
            return SimpleNamespace(stdout=json.dumps([self.current]).encode())
        if arguments[0] == "stop" and self.stop_sticks:
            self.current["State"] = {"Running": False, "Restarting": False, "Paused": False}
        return SimpleNamespace(stdout=b"")

    def abort(self):
        with patch.object(self.driver, "command", side_effect=self.command), \
                patch.object(self.driver, "status") as status:
            self.driver.abort_start(self.release, self.before)
            status.assert_not_called()

    def test_restarting_candidate_is_stopped_by_immutable_id_without_socket(self):
        self.abort()
        mutations = [args for args in self.commands if args[0] in {"update", "stop"}]
        self.assertEqual(mutations, [["stop", "--time", "30", self.identity],
                                     ["update", "--restart=no", self.identity]])
        self.assertFalse(self.current["State"]["Running"])
        self.assertFalse(self.current["State"]["Restarting"])

    def test_missing_candidate_and_previous_release_are_not_stopped(self):
        self.absent = True
        self.abort()
        self.assertEqual(len(self.commands), 1)
        self.absent = False
        self.current["Config"]["Labels"]["sfcloud.ai.release"] = self.before
        self.abort()
        self.assertFalse(any(args[0] in {"update", "stop"} for args in self.commands))

    def test_label_conflict_and_other_release_are_rejected(self):
        for field, value in (("sfcloud.ai.service", "other"), ("sfcloud.ai.release", "d" * 40)):
            with self.subTest(field=field):
                with patch.dict(self.current["Config"]["Labels"], {field: value}):
                    with self.assertRaises(ServiceError):
                        self.abort()
        self.assertFalse(any(args[0] in {"update", "stop"} for args in self.commands))

    def test_activation_evidence_or_malformed_record_blocks_stop(self):
        for name in ("active.json", "permit.json"):
            path = self.root / name
            for content in (json.dumps({"release": self.release}), "broken-json"):
                path.write_text(content)
                with self.assertRaises((ServiceError, ValueError)):
                    self.abort()
            path.unlink()
        self.assertFalse(any(args[0] in {"update", "stop"} for args in self.commands))

    def test_docker_errors_and_unconfirmed_termination_remain_blocking(self):
        for operation in ("container", "inspect", "update", "stop"):
            self.fail = operation
            with self.assertRaisesRegex(ServiceError, "docker_failure"):
                self.abort()
        self.fail, self.stop_sticks = None, False
        self.current["State"] = {"Running": False, "Restarting": True, "Paused": False}
        with self.assertRaisesRegex(ServiceError, "stop_not_confirmed"):
            self.abort()

    def test_policy_update_failure_after_stop_can_be_retried_without_socket(self):
        self.fail = "update"
        with self.assertRaisesRegex(ServiceError, "docker_failure"):
            self.abort()
        self.assertFalse(self.current["State"]["Running"])
        self.assertFalse(self.current["State"]["Restarting"])
        self.fail = None
        self.abort()
        self.assertEqual(self.commands[-1], ["inspect", self.identity])
