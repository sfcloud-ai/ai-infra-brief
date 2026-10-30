"""部署候选的离线回归；不读取真实状态、凭据或调用生产服务。"""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from deploy.release import ReleaseError, Releases, unpack_bundle, write_json
from deploy.relay_resume import assert_reconciled, binding, resume_class
from deploy.transport import ssh_arguments
from deploy.ssh_entry import parse_command
from deploy.stage_helper import docker_command
from test_relay_integration import relay, relay_fixtures


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Releases(self.root, "/usr/bin/python3")

    def ready(self, commit):
        path = self.root / "releases" / commit
        path.mkdir()
        write_json(path / "ready.json", {"commit": commit, "bundle_sha256": "d" * 64})

    def test_atomic_switch_and_explicit_rollback_keep_state(self):
        self.ready("a" * 40)
        self.ready("b" * 40)
        (self.root / "state-marker").write_text("do-not-rewind")
        with patch.object(self.manager, "health"):
            self.manager.activate("a" * 40)
            self.manager.activate("b" * 40)
            self.assertEqual(self.manager.pointer("previous"), "a" * 40)
            self.manager.activate("a" * 40)
        self.assertEqual(self.manager.pointer("current"), "a" * 40)
        self.assertEqual((self.root / "state-marker").read_text(), "do-not-rewind")

    def test_post_switch_failure_restores_old_pointer(self):
        self.ready("a" * 40)
        self.ready("b" * 40)
        self.manager.point("current", "a" * 40)
        with patch.object(self.manager, "health", side_effect=[None, ReleaseError("constructed_failure")]):
            with self.assertRaises(ReleaseError):
                self.manager.activate("b" * 40)
        self.assertEqual(self.manager.pointer("current"), "a" * 40)
        self.assertFalse((self.root / "pending.json").exists())

    def test_crash_journal_recovers_before_next_deploy(self):
        self.ready("a" * 40)
        self.ready("b" * 40)
        self.manager.point("current", "b" * 40)
        write_json(self.root / "pending.json", {"before": "a" * 40, "previous": None, "target": "b" * 40})
        self.manager.recover()
        self.assertEqual(self.manager.pointer("current"), "a" * 40)

    def test_reject_external_pointer_and_invalid_rollback_target(self):
        (self.root / "current").symlink_to("/tmp")
        with self.assertRaises(ReleaseError):
            self.manager.pointer("current")
        with self.assertRaises(ReleaseError):
            self.manager.activate("../../elsewhere")

    def archive(self, names):
        path = self.root / "candidate.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data, kind in names:
                info = tarfile.TarInfo(name)
                info.size, info.type = len(data), kind
                archive.addfile(info, io.BytesIO(data))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_reject_path_traversal_links_and_duplicate_members(self):
        for members in ([('manifest.json', b'{}', tarfile.REGTYPE), ('../escape', b'x', tarfile.REGTYPE)],
                        [('manifest.json', b'{}', tarfile.REGTYPE), ('wheels/x.whl', b'', tarfile.SYMTYPE)],
                        [('manifest.json', b'{}', tarfile.REGTYPE)] * 2):
            path, checksum = self.archive(members)
            with self.assertRaises(ReleaseError):
                unpack_bundle(path, self.root / "out", "a" * 40, checksum)
            self.assertFalse((self.root / "out").exists())

    def test_reject_tampered_bundle(self):
        path, _ = self.archive([('manifest.json', b'{}', tarfile.REGTYPE)])
        with self.assertRaises(ReleaseError):
            unpack_bundle(path, self.root / "out", "a" * 40, "0" * 64)

    def test_ssh_pins_host_and_rejects_command_injection(self):
        args = ssh_arguments("host.example.invalid", "deploy", "22", "/key", "/known", "a" * 40, "b" * 64)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("IdentityAgent=none", args)
        self.assertNotIn("accept-new", args)
        for host in ("-oProxyCommand=bad", "host;bad", "user@host"):
            with self.assertRaises(ValueError):
                ssh_arguments(host, "deploy", "22", "/key", "/known", "a" * 40, "b" * 64)

    def test_forced_command_and_helper_cannot_select_host_resources(self):
        self.assertEqual(parse_command("deploy " + "a" * 40 + " " + "b" * 64), ("a" * 40, "b" * 64))
        for command in ("sh", "deploy " + "a" * 40 + " " + "b" * 64 + "; id", "rollback " + "a" * 40):
            with self.assertRaises(ValueError):
                parse_command(command)
        config = {"root": "/opt/ai-infra-brief-prototype", "uid": 1000, "gid": 1000, "image": "sha256:" + "c" * 64}
        args = docker_command(config, "a" * 40, "b" * 64)
        self.assertIn("none", args)
        self.assertIn("--read-only", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--gpus", args)
        for changes in ({"root": "/"}, {"uid": 0}, {"image": "python:latest"}):
            with self.assertRaises(ValueError):
                docker_command({**config, **changes}, "a" * 40, "b" * 64)


class CursorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = relay.Store(Path(self.temp.name))
        self.addCleanup(self.store.db.close)
        self.store.set("cursor", relay_fixtures.ts(1))
        self.store.set("handoff_binding_v1", binding(relay_fixtures.CONFIG))
        self.slack, self.wecom = relay_fixtures.FakeSlack(), relay_fixtures.FakeWeCom()
        self.now = 1000.0
        self.receiver = resume_class(relay)(self.store, self.slack, self.wecom, relay_fixtures.CONFIG,
                                           clock=lambda: self.now, sleeper=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def test_resume_handles_gap_once_and_does_not_reset_to_latest(self):
        self.slack.messages = [relay_fixtures.brief(1), relay_fixtures.brief(2), relay_fixtures.brief(3)]
        self.receiver.baseline()
        self.receiver.poll()
        self.receiver.baseline()
        self.receiver.poll()
        self.assertEqual(len(self.wecom.sent), 2)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(3))
        self.assertNotIn("latest", self.slack.calls)

    def test_reconnect_preserves_gap_and_retries_fetch_only(self):
        self.receiver.baseline()
        self.slack.fail = True
        with self.assertRaises(relay.RelayError):
            self.receiver.poll()
        self.slack.fail = False
        self.slack.messages = [relay_fixtures.brief(2)]
        self.receiver.poll()
        self.receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertNotIn("latest", self.slack.calls)

    def test_identity_mismatch_or_missing_cursor_fails_closed(self):
        self.store.set("handoff_binding_v1", "different")
        with self.assertRaises(relay.RelayError):
            self.receiver.baseline()
        self.store.set("handoff_binding_v1", binding(relay_fixtures.CONFIG))
        self.store.set("cursor", "bad")
        with self.assertRaises(relay.RelayError):
            self.receiver.baseline()
        self.assertEqual(self.wecom.sent, [])

    def test_unknown_delivery_blocks_cutover_without_rewind(self):
        self.store.db.execute("INSERT INTO parts VALUES (?,?,?,?)", ("synthetic", 0, "hash", "unknown"))
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            assert_reconciled(self.store)
        self.assertEqual(self.store.get("cursor"), relay_fixtures.ts(1))


if __name__ == "__main__":
    unittest.main()
