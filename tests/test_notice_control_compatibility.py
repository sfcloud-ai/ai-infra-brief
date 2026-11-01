"""固定控制层与独立应用包的双向兼容回归，不连接真实服务。"""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import sqlite3
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from integrations.slack_wecom import runtime
from deploy.supervisor import ServiceError
from deploy.stage_helper import execute
from deploy.ssh_entry import parse_command
from test_cd_control import FakeDriver


ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "integrations/slack_wecom"


class NoticeControlCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous_path = sys.path[:]
        self.previous_modules = {name: sys.modules.pop(name, None) for name in ("images", "notice_format")}
        self.addCleanup(self.restore_imports)

    def restore_imports(self):
        sys.path[:] = self.previous_path
        for name, value in self.previous_modules.items():
            sys.modules.pop(name, None)
            if value is not None:
                sys.modules[name] = value

    def legacy(self, name):
        # 只保留旧应用的导入契约，不伪装为历史生产源码或真实投递测试。
        directory = self.root / name
        directory.mkdir()
        (directory / "relay.py").write_text('import images\nVERSION = "legacy"\n')
        (directory / "images.py").write_text('VERSION = "legacy-image"\n')
        return directory

    def test_old_control_loader_rejects_new_application_before_capabilities(self):
        # 重现旧固定 loader：执行应用前没有传入控制能力。
        spec = importlib.util.spec_from_file_location("managed_relay", CODE / "relay.py")
        module = importlib.util.module_from_spec(spec)
        with self.assertRaisesRegex(ValueError, "administrator_control_upgrade_required"):
            spec.loader.exec_module(module)
        self.assertNotIn("images", sys.modules)
        self.assertNotIn("notice_format", sys.modules)

    def test_capability_failure_does_not_stop_existing_sender_or_rewind_ledger(self):
        driver = FakeDriver()
        original = dict(driver.ledger)
        with patch.object(driver, "preflight", side_effect=ServiceError("administrator_control_upgrade_required")):
            with self.assertRaisesRegex(ServiceError, "administrator_control_upgrade_required"):
                execute(parse_command("deploy 10 " + "a" * 40 + " " + "b" * 64),
                        {"live_enabled": True}, self.root,
                        stage_runner=lambda *args: None, driver_factory=lambda *args: driver)
        self.assertEqual(driver.running, driver.legacy)
        self.assertEqual(driver.ledger, original)
        self.assertFalse(any(event[0] in {"stop", "handoff", "start_paused"} for event in driver.events))

    def test_runtime_only_upgrade_cannot_claim_missing_resume_capability(self):
        with patch.object(runtime.resume_adapter, "CONTROL_CAPABILITIES", frozenset()):
            with self.assertRaisesRegex(ValueError, "administrator_control_upgrade_required"):
                runtime.load_module(CODE)

    def test_updated_control_loads_current_application_with_pinned_dependency(self):
        module = runtime.load_module(CODE)
        self.assertEqual(Path(module.notice_format.__file__).resolve(), CODE / "notice_format.py")
        self.assertIn("article_notice_read_v1", module.CONTROL_CAPABILITIES)

    def test_legacy_snapshot_and_old_release_do_not_require_notice_dependency(self):
        for name in ("legacy", "retained-release"):
            with self.subTest(layout=name):
                sys.modules.pop("images", None)
                directory = self.legacy(name)
                module = runtime.load_module(directory)
                self.assertEqual(module.VERSION, "legacy")
                self.assertFalse(hasattr(module, "notice_format"))
                self.assertFalse((directory / "notice_format.py").exists())

    def test_old_code_supports_every_control_entrypoint_without_new_dependency(self):
        directory = self.legacy("legacy")
        cases = [("check-state", [], "check_state"), ("handoff", [], "handoff"),
                 ("serve", [], "serve"), ("control", ["--operation", "pause", "--deadline", "5"], "control"),
                 ("control", ["--operation", "activate", "--deadline", "5"], "control"),
                 ("control", ["--operation", "status", "--deadline", "5"], "control")]
        with patch.object(sys, "argv", ["runtime.py", "capabilities", "--code", str(directory)]):
            output = io.StringIO()
            with redirect_stdout(output):
                runtime.main()
            self.assertIn('"pause_activate":true', output.getvalue())
        for command, extra, called in cases:
            with self.subTest(command=command, extra=extra):
                args = ["runtime.py", command, "--code", str(directory), "--root", str(self.root),
                        "--release", "a" * 40, *extra]
                with ExitStack() as stack:
                    mocks = {name: stack.enter_context(patch.object(runtime, name))
                             for name in ("check_state", "handoff", "serve", "control")}
                    stack.enter_context(patch.object(sys, "argv", args))
                    runtime.main()
                    mocks[called].assert_called_once()
                    self.assertEqual(mocks[called].call_args.args[0].VERSION, "legacy")
                    for name, mock in mocks.items():
                        if name != called:
                            mock.assert_not_called()

    def test_abrupt_preflight_process_exit_leaves_same_message_recoverable(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        script = r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import relay as r
import resume
from test_article_notice import message,manifest,NOTICE_CONFIG,PNG
from test_relay import FakeSlack,FakeWeCom,ts
store=r.Store(Path(sys.argv[2]))
if sys.argv[3]=="crash":
    store.set("cursor",ts(0));store.set("handoff_binding_v1",resume.binding(NOTICE_CONFIG))
else:
    assert store.get("cursor")==ts(0)
slack,wecom=FakeSlack(),FakeWeCom()
slack.messages=[message()]
slack.download_image=(lambda plan:os._exit(23)) if sys.argv[3]=="crash" else (lambda plan:PNG)
images=[]
wecom.send_image=lambda data,mime:images.append(data) or "sent"
now=[1000.0]
receiver=resume.resume_class(r)(store,slack,wecom,NOTICE_CONFIG,clock=lambda:now[0],sleeper=lambda seconds:now.__setitem__(0,now[0]+seconds),notice_reader=SimpleNamespace(read=lambda candidate:manifest()))
receiver.poll();receiver.poll()
assert len(wecom.sent)==1 and len(images)==1 and store.get("cursor")==ts(1)
print(json.dumps({"synthetic_texts":len(wecom.sent),"synthetic_images":len(images),"status":store.status("auto:"+ts(1))["status"]}))
'''
        args = [sys.executable, "-B", "-c", script, str(CODE), str(state)]
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        stopped = subprocess.run([*args, "crash"], cwd=self.root, env=environment, capture_output=True, timeout=15)
        self.assertEqual(stopped.returncode, 23, stopped.stderr)
        with sqlite3.connect(state / "state.sqlite3") as database:
            self.assertEqual(database.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT count(*) FROM image_parts").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT count(*) FROM meta WHERE key LIKE 'notice_edition:%'").fetchone()[0], 0)
        recovered = subprocess.run([*args, "recover"], cwd=self.root, env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout), {"synthetic_texts": 1, "synthetic_images": 1, "status": "sent"})

    def test_optional_dependency_still_rejects_cached_foreign_module(self):
        other = self.root / "other"
        other.mkdir()
        (other / "notice_format.py").write_text("VALUE = 1\n")
        spec = importlib.util.spec_from_file_location("notice_format", other / "notice_format.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 新应用实际需要的模块缺少接口或不属于 release 时都不能接受。
        sys.modules["notice_format"] = module
        with self.assertRaises((AttributeError, ValueError)):
            runtime.load_module(CODE)


if __name__ == "__main__":
    unittest.main()
