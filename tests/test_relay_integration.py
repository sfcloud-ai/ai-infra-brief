"""晨报与用户提供转发器的离线契约回归，不接触真实会话或传输。"""

import ast
import hashlib
import http.client
import json
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ai_brief.contracts import RESOURCE_ROOT
from ai_brief.pipeline import run
from ai_brief.relay_contract import prepare_relay_preview
from ai_brief.storage import BriefError, read_json

ROOT = Path(__file__).resolve().parents[1]
RELAY_ROOT = ROOT / "integrations" / "slack_wecom"
sys.path.insert(0, str(RELAY_ROOT))
import relay
import test_images as image_fixtures
import test_relay as relay_fixtures
sys.path.pop(0)


class StripDocumentation(ast.NodeTransformer):
    def visit_Module(self, node):
        return self.trim(self.generic_visit(node))

    def visit_ClassDef(self, node):
        return self.trim(self.generic_visit(node))

    def visit_FunctionDef(self, node):
        return self.trim(self.generic_visit(node))

    def visit_AsyncFunctionDef(self, node):
        return self.trim(self.generic_visit(node))

    def trim(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
        return node


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        result = run(self.root / "pipeline", RESOURCE_ROOT / "fixtures/empty.json",
                     as_of="2026-10-30T08:00:00+08:00", allow_backfill=True)
        self.brief = read_json(Path(result["run_dir"]) / "brief.json")
        state = self.root / "relay-state"
        state.mkdir(mode=0o700)
        self.store = relay.Store(state)
        self.addCleanup(self.store.db.close)
        self.slack = relay_fixtures.FakeSlack()
        self.wecom = relay_fixtures.FakeWeCom()
        self.images = []
        self.slack.download_image = lambda plan: image_fixtures.PNG
        self.wecom.send_image = lambda data, mime: self.images.append((data, mime)) or "sent"
        self.now = 1000.0
        self.receiver = relay.Relay(self.store, self.slack, self.wecom,
                                    {**relay_fixtures.CONFIG, "forward_images": True},
                                    clock=lambda: self.now, sleeper=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def event(self, preview, **changes):
        return {**preview["event_template"], "user": "UDOT", "ts": relay_fixtures.ts(1), **changes}

    def checked_runner(self, *arguments):
        result = subprocess.run([sys.executable, "-B", str(RELAY_ROOT / "run_tests.py"), *arguments],
                                cwd=self.root, env={"HOME": str(self.root), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                                text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_original_and_connector_tests_run_with_outbound_guard(self):
        result = self.checked_runner()
        self.assertEqual(result["tests_run"], 122)  # 原有 114 项及本机校验入口 8 项。
        self.assertEqual(result["outbound_attempts"], 0)
        if result["skipped"]:
            self.assertEqual(result["skipped"], 1)
            self.skipTest("原 Unix socket 项因沙箱能力不足跳过，其余项目通过；CI 严格执行。")

    def test_outbound_guard_rejects_socket_and_dns_before_network(self):
        result = self.checked_runner("--self-test-guard")
        self.assertEqual(result["guard_self_test"], "passed")
        self.assertEqual(result["blocked_before_network"], 2)

    def test_original_runtime_and_fixtures_preserved_except_documented_changes(self):
        provenance = read_json(RELAY_ROOT / "INTEGRATION.json")
        expected = provenance["original_ast_sha256_without_docstrings"]
        reviewed = provenance["reviewed_notice_ast_sha256_without_docstrings"]
        for filename, fingerprint in expected.items():
            tree = StripDocumentation().visit(ast.parse((RELAY_ROOT / filename).read_text()))
            if filename in reviewed:
                # 原始来源指纹保留在清单中；已审查的运行时更新改为固定完整新 AST。
                actual = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
                self.assertEqual(actual, reviewed[filename], filename)
                continue
            if filename == "relay.py":
                # 包入口仅切换相对导入，独立旧入口仍使用原始 images 导入。
                tree.body = [node.orelse[0] if isinstance(node, ast.If)
                             and isinstance(node.test, ast.Name) and node.test.id == "__package__"
                             else node for node in tree.body]
                matches = [node for node in ast.walk(tree) if isinstance(node, ast.Tuple) and
                           all(isinstance(value, ast.Constant) for value in node.elts) and
                           [value.value for value in node.elts] == [None, "bot_message", "file_share"]]
                self.assertEqual(len(matches), 1)
                matches[0].elts.pop()
                # 移除已单独审查并有真实本地 I/O 回归覆盖的超时增强，再比较原始指纹。
                tree.body = [node for node in tree.body
                             if not (isinstance(node, ast.Import) and [alias.name for alias in node.names] == ["signal"])
                             and not (isinstance(node, ast.FunctionDef) and node.name == "image_download_timeout")
                             and not (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "IMAGE_DOWNLOAD_TIMEOUT" for target in node.targets))]
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and node.name == "Handler":
                        node.body = [child for child in node.body if not (isinstance(child, ast.Assign)
                                     and any(isinstance(target, ast.Name) and target.id == "timeout" for target in child.targets))]
                    if isinstance(node, ast.With):
                        node.items = [item for item in node.items if not (isinstance(item.context_expr, ast.Call)
                                      and isinstance(item.context_expr.func, ast.Name)
                                      and item.context_expr.func.id == "image_download_timeout")]
            actual = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
            self.assertEqual(actual, fingerprint, filename)

    def test_preview_title_matches_receiver_without_fake_identity(self):
        preview = prepare_relay_preview(self.brief)
        self.assertTrue(preview["event_template"]["text"].startswith("AI Infra 晨报"))
        self.assertNotIn("user", preview["event_template"])
        self.assertNotIn("ts", preview["event_template"])
        self.assertFalse(preview["delivery_allowed"])
        self.assertTrue(self.receiver.is_brief(self.event(preview)))

    def test_file_share_text_image_contract_and_dedup(self):
        preview = prepare_relay_preview(self.brief, [image_fixtures.attachment()])
        self.assertEqual(preview["event_template"]["subtype"], "file_share")
        self.receiver.baseline()
        self.slack.messages.append(self.event(preview))
        self.receiver.poll()
        self.receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.images, [(image_fixtures.PNG, "image/png")])
        self.assertEqual(self.store.status("auto:" + relay_fixtures.ts(1))["status"], "sent")

    def test_file_share_keeps_sender_title_thread_and_edit_filters(self):
        preview = prepare_relay_preview(self.brief, [image_fixtures.attachment()])
        for changes in ({"user": "UOTHER"}, {"text": "普通聊天"}, {"thread_ts": relay_fixtures.ts(0)},
                        {"edited": {"ts": relay_fixtures.ts(2)}}, {"subtype": "message_changed"}):
            self.assertFalse(self.receiver.is_brief(self.event(preview, **changes)))

    def test_svg_requires_explicit_rasterization_and_upload(self):
        with self.assertRaisesRegex(BriefError, "PNG/JPEG"):
            prepare_relay_preview(self.brief, [{"mimetype": "image/svg+xml"}])

    def test_original_receiver_rejects_untrusted_attachment_url(self):
        file = image_fixtures.attachment(url_private="https://attacker.invalid/untrusted.png")
        self.slack.download_image = lambda plan: self.fail("untrusted image must not be downloaded")
        self.receiver.baseline()
        self.slack.messages.append(self.event(prepare_relay_preview(self.brief, [file])))
        self.receiver.poll()
        self.assertEqual(self.images, [])
        self.assertEqual(self.store.status("auto:" + relay_fixtures.ts(1))["status"], "partial")


class RelayTimeoutTests(unittest.TestCase):
    def test_incomplete_local_http_request_releases_server(self):
        with tempfile.TemporaryDirectory(prefix="relay-timeout-") as directory:
            path = str(Path(directory) / "test.sock")
            with patch.object(relay.Handler, "timeout", 0.1):
                with socketserver.UnixStreamServer(path, relay.Handler) as server:
                    worker = threading.Thread(target=server.handle_request, daemon=True)
                    worker.start()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(1)
                        client.connect(path)
                        client.sendall(b"POST /status HTTP/1.1\r\nX-Incomplete: ")
                        self.assertEqual(client.recv(1), b"")
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())

    def test_trickling_image_read_obeys_total_deadline_and_restores_alarm(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        stopped = threading.Event()
        plan = image_fixtures.im.validate_file(image_fixtures.attachment(), "TTEST")

        class Response(http.client.HTTPResponse):
            def geturl(self):
                return plan["url"]

        def trickle():
            try:
                writer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: "
                               + str(len(image_fixtures.PNG)).encode() + b"\r\n\r\n")
                for byte in image_fixtures.PNG:
                    if stopped.wait(0.02):
                        break
                    writer.sendall(bytes([byte]))
            except OSError:
                pass

        worker = threading.Thread(target=trickle, daemon=True)
        worker.start()
        response = Response(reader)
        response.begin()
        slack = image_fixtures.ImageTransportTests().slack()
        previous = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        try:
            with patch.object(slack.http, "open", return_value=response), patch.object(relay, "IMAGE_DOWNLOAD_TIMEOUT", 0.12):
                with self.assertRaisesRegex(image_fixtures.im.ImageError, "image_download_timed_out"):
                    slack.download_image(plan)
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        finally:
            stopped.set()
            response.close()
            worker.join(timeout=1)
