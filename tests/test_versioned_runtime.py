"""应用、控制运行时与恢复适配器必须使用同一 release。"""

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from deploy.supervisor import DockerDriver, ServiceError


class VersionedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.driver = DockerDriver.__new__(DockerDriver)
        self.driver.config = {"root": str(self.root), "image": "synthetic-image"}
        self.driver.live = {"container": "synthetic-managed", "legacy_container": "synthetic-old"}
        self.driver.legacy_path = self.root / "legacy.json"
        self.driver.activation = self.root / "activation"
        self.driver.activation.mkdir()
        self.release = "a" * 40

    def test_runtime_and_rollback_select_the_corresponding_package(self):
        for release in ("a" * 40, "b" * 40):
            self.assertEqual(self.driver.runtime(release), self.driver.code(release) + "/runtime.py")
        self.driver.legacy_path.write_text(json.dumps({"release": "c" * 40}))
        self.assertEqual(self.driver.runtime("c" * 40), "/control/runtime.py")
        with self.assertRaisesRegex(ServiceError, "invalid_release"):
            self.driver.runtime("../../other")

    def test_capability_failure_happens_in_the_candidate_runtime(self):
        directory = self.root / "releases" / self.release
        directory.mkdir(parents=True)
        (directory / "ready.json").write_text(json.dumps({"commit": self.release}))
        with patch.object(self.driver, "one_shot", side_effect=ServiceError("incompatible_runtime")) as run:
            with self.assertRaisesRegex(ServiceError, "incompatible_runtime"):
                self.driver.preflight(self.release)
        self.assertEqual(run.call_args.kwargs,
                         {"readonly_state": True, "runtime": self.driver.runtime(self.release)})

    def test_handoff_and_control_use_selected_release_without_state_rewind(self):
        with patch.object(self.driver, "one_shot") as run:
            self.driver.handoff(self.release)
        self.assertEqual(run.call_args.kwargs["runtime"], self.driver.runtime(self.release))
        with patch.object(self.driver, "running_release", return_value=self.release), \
                patch.object(self.driver, "command", return_value=SimpleNamespace(stdout=b"{}")) as run:
            self.driver.status(self.release)
        self.assertEqual(run.call_args.args[0][3], self.driver.runtime(self.release))

    def test_start_uses_versioned_runtime_and_retains_pause_before_activation(self):
        with patch.object(self.driver, "running_release", return_value=None), \
                patch.object(self.driver, "inspect", return_value=None), \
                patch.object(self.driver, "common", return_value=[]), \
                patch.object(self.driver, "revoke_permit"), \
                patch.object(self.driver, "command") as run:
            self.driver.start_paused(self.release)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("python") + 1], self.driver.runtime(self.release))
        self.assertIn("serve", command)
        self.assertIn("--activation-file", command)
        self.assertNotIn("activate", command)


if __name__ == "__main__":
    unittest.main()
