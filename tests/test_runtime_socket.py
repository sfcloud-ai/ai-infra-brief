"""通过真实子进程和 Unix socket 检查控制入口，外部传输替换为本地合成队列。"""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from test_relay_integration import ROOT, relay, relay_fixtures
from deploy.relay_resume import binding


CHILD = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from integrations.slack_wecom import relay,runtime
root=Path(sys.argv[2])
class Slack:
    def __init__(self,*args):pass
    def verify(self):pass
    def latest(self):raise AssertionError("cursor_reset_forbidden")
    def newer(self,cursor):
        return [item for item in json.loads((root/"queue.json").read_text()) if item["ts"]>cursor]
class WeCom:
    def __init__(self,*args):pass
    def send(self,text):
        with (root/"synthetic-sent.txt").open("a") as stream:stream.write(json.dumps(text)+"\n")
        return "sent"
relay.Slack=Slack
relay.WeCom=WeCom
relay.load_secrets=lambda root:{"slack_user_token":"synthetic","wecom_webhook":"synthetic"}
runtime.serve(relay,root,"a"*40)
'''


class RuntimeSocketTests(unittest.TestCase):
    def test_pause_activate_restart_preserves_cursor_and_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("state", "run", "secrets"):
                (root / name).mkdir(mode=0o700)
            config = root / "config.json"
            config.write_text(json.dumps(relay_fixtures.CONFIG))
            config.chmod(0o600)
            store = relay.Store(root / "state")
            store.set("cursor", relay_fixtures.ts(1))
            store.set("handoff_binding_v1", binding(relay_fixtures.CONFIG))
            store.db.close()
            queue = root / "queue.json"
            queue.write_text(json.dumps([relay_fixtures.brief(2)]))
            script = root / "synthetic_server.py"
            script.write_text(CHILD)

            def count_sent():
                path = root / "synthetic-sent.txt"
                return len(path.read_text().splitlines()) if path.exists() else 0

            def control(operation):
                connection = relay.UnixConnection(root / "run/relay.sock")
                connection.request("POST", "/control/" + operation,
                                   body=json.dumps({"release": "a" * 40}), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                data = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                return data

            def wait(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.02)
                self.fail("synthetic_runtime_deadline")

            for expected in (1, 2):
                child = subprocess.Popen([sys.executable, "-B", str(script), str(ROOT), str(root)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         env={"PATH": "/usr/bin:/bin", "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
                try:
                    wait(lambda: (root / "run/relay.sock").exists() or child.poll() is not None)
                    self.assertIsNone(child.poll(), child.stderr.read().decode() if child.poll() is not None else "")
                    self.assertEqual(control("status")["phase"], "paused")
                    self.assertEqual(count_sent(), expected - 1)
                    with self.assertRaises(relay.RelayError):
                        with relay.exclusive(root):
                            pass
                    control("activate")
                    wait(lambda: count_sent() == expected)
                    paused = control("pause")
                    self.assertEqual(paused["phase"], "paused")
                    self.assertTrue(paused["reconciled"])
                    queue.write_text(json.dumps([relay_fixtures.brief(2), relay_fixtures.brief(3)]))
                    time.sleep(0.1)
                    self.assertEqual(count_sent(), expected)
                finally:
                    child.terminate()
                    child.wait(timeout=10)
                    child.stdout.close()
                    child.stderr.close()
                self.assertEqual(child.returncode, 0)
                with relay.exclusive(root):
                    pass
            store = relay.Store(root / "state")
            try:
                self.assertEqual(store.get("cursor"), relay_fixtures.ts(3))
            finally:
                store.db.close()
