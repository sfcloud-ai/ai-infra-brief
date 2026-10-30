"""新转发包向旧接收器发布时，必须在上传任何字节之前阻断。"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deploy import transport
from deploy.stage_helper import execute


class ReceiverCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "relay.tar.gz"
        self.bundle.write_bytes(b"synthetic-relay-only-bundle")
        self.environment = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main",
                            "GITHUB_REPOSITORY": "sfcloud-ai/ai-infra-brief", "GITHUB_SHA": "a" * 40,
                            "GITHUB_RUN_NUMBER": "1", "RUNNER_TEMP": str(self.root),
                            "T4_SSH_HOST": "host.example.invalid", "T4_SSH_USER": "deploy", "T4_SSH_PORT": "22",
                            "T4_SSH_PRIVATE_KEY": "synthetic-private-key", "T4_SSH_KNOWN_HOSTS": "synthetic-host-record"}

    def test_legacy_or_unknown_receiver_never_receives_upload(self):
        for status in ({"sequence": 1, "active": "b" * 40, "previous": None},
                       {"receiver_format": "unknown"}, None):
            with self.subTest(status=status), patch.dict(os.environ, self.environment, clear=True), \
                    patch("sys.argv", ["transport.py", str(self.bundle)]), \
                    patch.object(transport.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps(status).encode())) as run:
                with self.assertRaises(transport.ReceiverUpgradeRequired):
                    transport.main()
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][-1], "status")
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_upgraded_receiver_allows_verified_status_then_upload(self):
        status = execute(("status",), {"live_enabled": False}, self.root / "control")
        self.assertEqual(status["receiver_format"], "relay-only-v1")
        operations = []

        def run(arguments, **kwargs):
            operations.append(arguments[-1].split()[0])
            self.assertFalse(any(name.startswith("T4_") for name in kwargs["env"]))
            if operations == ["status"]:
                result = status
            else:
                self.assertEqual(kwargs["stdin"].read(), self.bundle.read_bytes())
                result = {"status": "activated", "commit": "a" * 40}
            return SimpleNamespace(returncode=0, stdout=json.dumps(result).encode())

        with patch.dict(os.environ, self.environment, clear=True), \
                patch("sys.argv", ["transport.py", str(self.bundle)]), \
                patch.object(transport.subprocess, "run", side_effect=run), redirect_stdout(io.StringIO()):
            transport.main()
        self.assertEqual(operations, ["status", "deploy"])
