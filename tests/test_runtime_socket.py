"""通过真实子进程和 Unix socket 检查控制入口，外部传输替换为本地合成队列。"""

import contextlib
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
from deploy.supervisor import DockerDriver, ServiceError


CHILD = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from integrations.slack_wecom import relay,runtime
root=Path(sys.argv[2])
class Slack:
    def __init__(self,*args):self.reads=0
    def verify(self):pass
    def latest(self):raise AssertionError("cursor_reset_forbidden")
    def newer(self,cursor):
        self.reads+=1
        if (root/"fail-on-poll").exists() and self.reads==3:
            raise relay.RelayError("slack_request_failed",502)
        return [item for item in json.loads((root/"queue.json").read_text()) if item["ts"]>cursor]
class WeCom:
    def __init__(self,*args):pass
    def send(self,text):
        with (root/"synthetic-sent.txt").open("a") as stream:stream.write(json.dumps(text)+"\n")
        return "sent"
relay.Slack=Slack
relay.WeCom=WeCom
relay.load_secrets=lambda root:{"slack_user_token":"synthetic","wecom_webhook":"synthetic"}
original_config=relay.load_config
def config(path):
    value=original_config(path)
    if (root/"fast-poll").exists():value["poll_seconds"]=0.05
    return value
relay.load_config=config
runtime.serve(relay,root,"a"*40)
'''


@contextlib.contextmanager
def synthetic_runtime(messages, flags=()):
    """启动真实运行循环；只有外部提供方与可选轮询间隔是测试替身。"""
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
        (root / "queue.json").write_text(json.dumps(messages))
        for flag in flags:
            (root / flag).touch()
        script = root / "synthetic_server.py"
        script.write_text(CHILD)
        child = subprocess.Popen([sys.executable, "-B", str(script), str(ROOT), str(root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={"PATH": "/usr/bin:/bin", "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            deadline = time.monotonic() + 10
            while not (root / "run/relay.sock").exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("synthetic_server_start_failed")
                time.sleep(0.02)
            yield root, child
        finally:
            child.terminate()
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()


def local_control_driver(root):
    """保留实际宿主截止时间和控制子进程，仅把 Docker exec 映射到本机子进程。"""
    driver = DockerDriver.__new__(DockerDriver)
    driver.live = {"container": "synthetic"}
    driver.running_release = lambda: "a" * 40
    driver.code = lambda release: str(ROOT / "integrations/slack_wecom")
    def command(arguments, timeout):
        control_args = list(arguments[4:])
        control_args[control_args.index("--root") + 1] = str(root)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "integrations/slack_wecom/runtime.py")]
                                + control_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise ServiceError("synthetic_control_process_failed")
        return result
    driver.command = command
    return driver


class RuntimeSocketTests(unittest.TestCase):
    def test_real_loop_recovers_transient_history_read_without_reactivation(self):
        with synthetic_runtime([relay_fixtures.brief(2)], ("fast-poll", "fail-on-poll")) as (root, child):
            driver = local_control_driver(root)
            driver.status("a" * 40, "activate")
            deadline = time.monotonic() + 10
            path = root / "synthetic-sent.txt"
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(path.exists())
            status = driver.status("a" * 40, "pause")
            self.assertTrue(status["reconciled"])
            self.assertFalse(status["in_flight"])
            self.assertEqual(len(path.read_text().splitlines()), 1)
            self.assertIsNone(child.poll())
            store = relay.Store(root / "state")
            try:
                self.assertEqual(store.get("cursor"), relay_fixtures.ts(2))
            finally:
                store.db.close()

    def test_long_delivery_drains_beyond_old_timeout_before_next_message(self):
        text = "AI Infra 晨报\n" + "x" * 60000
        messages = [relay_fixtures.brief(2, text=text), relay_fixtures.brief(3)]
        with synthetic_runtime(messages) as (root, child):
            driver = local_control_driver(root)
            driver.status("a" * 40, "activate")
            path = root / "synthetic-sent.txt"
            deadline = time.monotonic() + 10
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(path.exists())
            started = time.monotonic()
            status = driver.status("a" * 40, "pause")
            self.assertGreater(time.monotonic() - started, 90)
            self.assertEqual(status["phase"], "paused")
            self.assertTrue(status["reconciled"])
            self.assertFalse(status["in_flight"])
            self.assertEqual(len(path.read_text().splitlines()), len(relay.chunks(text)))
            self.assertIsNone(child.poll())
            store = relay.Store(root / "state")
            try:
                self.assertEqual(store.get("cursor"), relay_fixtures.ts(2))
                self.assertEqual(store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)
            finally:
                store.db.close()

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
                                   body=json.dumps({"release": "a" * 40, "deadline": time.monotonic() + 180,
                                                    "nonce": None}), headers={"Content-Type": "application/json"})
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
