"""校验器 CD 的失败、隔离、旧版本恢复与固定身份边界。"""

import json
import errno
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from deploy import notice_service as service
from deploy.supervisor import DockerDriver


class NoticeServiceCDTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.before, self.target = "a" * 40, "b" * 40
        self.config = {"root": str(self.root), "relay_root": "/synthetic-reader",
                       "ipc": "/synthetic-ipc", "image": "sha256:" + "d" * 64,
                       "uid": 1000, "gid": 1000, "publisher_uid": 996, "name": "synthetic-checker"}
        for release in (self.before, self.target):
            directory = self.root / "releases" / release
            directory.mkdir(parents=True)
            (directory / "ready.json").write_text(json.dumps({"commit": release}))
        self.record = self.root / "release.json"
        self.record.write_text(json.dumps({"release": self.before}))

    def test_preflight_has_neither_reader_credentials_nor_network(self):
        args = service.container_command(self.config, self.target, preflight=True)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")
        self.assertNotIn("/synthetic-reader", " ".join(args))
        self.assertNotIn("/synthetic-ipc", " ".join(args))
        self.assertIn("--read-only", args)
        live = service.container_command(self.config, self.target)
        self.assertIn("type=bind,src=/synthetic-reader,dst=/relay,readonly", live)
        self.assertEqual(live[live.index("--publisher-uid") + 1], "996")
        self.assertNotIn("--publish", live)
        self.assertNotIn("--privileged", live)

    def test_missing_or_changed_ready_proof_cannot_select_a_release(self):
        for release in ("../escape", "c" * 40):
            with self.assertRaises(ValueError):
                service.release_directory(self.config, release)
        path = self.root / "releases" / self.target / "ready.json"
        path.write_text(json.dumps({"commit": self.before}))
        with self.assertRaisesRegex(ValueError, "not_ready"):
            service.container_command(self.config, self.target)

    def test_container_identity_conflict_never_stops_another_container(self):
        item = {"Name": "/synthetic-checker", "Image": self.config["image"],
                "Config": {"Labels": {service.LABEL: self.target}, "User": "1000:1000"}}
        with patch.object(service, "command", side_effect=["c" * 64, json.dumps([item])]) as command:
            with self.assertRaisesRegex(ValueError, "identity_conflict"):
                service.inspect(self.config, self.before)
        self.assertEqual(command.call_count, 2)
        self.assertFalse(any("stop" in call.args[0] for call in command.call_args_list))

    def run_switch(self, operation):
        with patch.object(service, "load_config", return_value=self.config), \
                patch.object(service, "admin_file"), patch.object(service, "RECORD", self.record), \
                patch.object(service, "preflight_container"):
            return operation()

    def test_switch_records_target_only_after_confirmed_old_container_exit(self):
        events = []
        def command(args, **kwargs):
            events.append((args, json.loads(self.record.read_text())["release"]))
            return ""
        with patch.object(service, "command", side_effect=command), \
                patch.object(service, "inspect", side_effect=[{"Id": "owned"}, None]), \
                patch.object(service, "ready", return_value=True):
            self.run_switch(lambda: service.switch(self.target))
        self.assertEqual(events[-2][0], ["/bin/systemctl", "stop", service.SERVICE])
        self.assertEqual(events[-2][1], self.before)
        self.assertEqual(events[-1], (["/bin/systemctl", "start", service.SERVICE], self.target))

    def test_unconfirmed_stop_leaves_old_record_and_never_starts_candidate(self):
        with patch.object(service, "command") as command, \
                patch.object(service, "inspect", return_value={"Id": "still-present"}):
            with self.assertRaisesRegex(ValueError, "stop_unconfirmed"):
                self.run_switch(lambda: service.switch(self.target))
        self.assertEqual(json.loads(self.record.read_text()), {"release": self.before})
        self.assertFalse(any(call.args[0][:2] == ["/bin/systemctl", "start"] for call in command.call_args_list))

    def test_preflight_failure_leaves_record_and_service_untouched(self):
        with patch.object(service, "load_config", return_value=self.config), \
                patch.object(service, "preflight_container", side_effect=ValueError("bad_candidate")), \
                patch.object(service, "command") as command:
            with self.assertRaisesRegex(ValueError, "bad_candidate"):
                service.switch(self.target)
        command.assert_not_called()
        self.assertEqual(json.loads(self.record.read_text()), {"release": self.before})

    def test_timed_out_preflight_removes_only_its_identified_container(self):
        commands = []
        def command(args, **kwargs):
            commands.append(args)
            if args[1] == "run":
                raise subprocess.TimeoutExpired(args, 60)
            return ""
        with patch.object(service, "command", side_effect=command), \
                patch.object(service, "inspect", return_value={"Id": "owned-id"}) as inspect:
            with self.assertRaises(subprocess.TimeoutExpired):
                service.preflight_container(self.config, self.target)
        name = commands[0][commands[0].index("--name") + 1]
        self.assertTrue(name.startswith("ai-brief-notice-preflight-"))
        self.assertNotEqual(name, self.config["name"])
        self.assertEqual(inspect.call_args.args[0]["name"], name)
        self.assertEqual(commands[-1], ["/usr/bin/docker", "rm", "-f", "owned-id"])

    def test_preflight_identity_conflict_does_not_remove_container(self):
        with patch.object(service, "command") as command, \
                patch.object(service, "inspect", side_effect=ValueError("identity_conflict")):
            with self.assertRaisesRegex(ValueError, "identity_conflict"):
                service.preflight_container(self.config, self.target)
        self.assertEqual(command.call_count, 1)

    def test_same_release_unavailable_socket_is_restarted(self):
        for error in (FileNotFoundError(errno.ENOENT, "missing"),
                      ConnectionRefusedError(errno.ECONNREFUSED, "not listening")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(service, "command") as command, \
                    patch.object(service, "inspect", return_value=None), \
                    patch.object(service, "ready", side_effect=[error, True]):
                self.run_switch(lambda: service.switch(self.before))
            self.assertIn(["/bin/systemctl", "start", service.SERVICE],
                          [call.args[0] for call in command.call_args_list])

    def test_same_release_identity_error_is_not_treated_as_socket_race(self):
        with patch.object(service, "command") as command, \
                patch.object(service, "ready", side_effect=ValueError("identity_conflict")):
            with self.assertRaisesRegex(ValueError, "identity_conflict"):
                self.run_switch(lambda: service.switch(self.before))
        command.assert_not_called()

    def test_failed_new_start_can_switch_back_without_restoring_any_ledger(self):
        def fail_start(args, **kwargs):
            if args[:2] == ["/bin/systemctl", "start"]:
                raise ValueError("start_failed")
            return ""
        with patch.object(service, "command", side_effect=fail_start), \
                patch.object(service, "inspect", return_value=None):
            with self.assertRaisesRegex(ValueError, "start_failed"):
                self.run_switch(lambda: service.switch(self.target))
        self.assertEqual(json.loads(self.record.read_text()), {"release": self.target})
        with patch.object(service, "command"), patch.object(service, "inspect", return_value=None), \
                patch.object(service, "ready", return_value=True):
            self.run_switch(lambda: service.switch(self.before))
        self.assertEqual(json.loads(self.record.read_text()), {"release": self.before})

    def test_health_failure_does_not_write_relay_activation_record(self):
        driver = DockerDriver.__new__(DockerDriver)
        driver.live = {"notice_service": True}
        driver.activation = self.root
        with patch.object(driver, "wait"), \
                patch.object(service, "switch", side_effect=ValueError("checker_failed")), \
                patch.object(driver, "revoke_permit") as revoke:
            with self.assertRaisesRegex(ValueError, "checker_failed"):
                driver.wait_healthy(self.target)
        self.assertFalse((self.root / "active.json").exists())
        revoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
