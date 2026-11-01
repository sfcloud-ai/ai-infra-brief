import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

if __package__:
    from . import notice_client, notice_socket
else:
    import notice_client
    import notice_socket


class RequestBoundaryTests(unittest.TestCase):
    def test_unsupported_fields_never_start_checker(self):
        with patch.object(subprocess, "run") as run:
            for request in ({}, {"message_ts": "1", "file_id": "F1", "notice_manifest": {}, "executable": "bad"}):
                with self.assertRaises(ValueError):
                    notice_socket.invoke(json.dumps(request).encode(), Path("/fixed/reader"))
            run.assert_not_called()

    def test_fixed_invocation_ignores_inherited_credentials_and_commands(self):
        result = subprocess.CompletedProcess([], 1, json.dumps(notice_socket.NEGATIVE).encode())
        data = b'{"message_ts":"1800000000.000001","file_id":"F123","notice_manifest":{}}'
        with patch.object(subprocess, "run", return_value=result) as run:
            self.assertEqual(notice_socket.invoke(data, Path("/fixed/reader")), notice_socket.NEGATIVE)
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:], ["-I", "-B", "-m", "ai_brief_relay.notice_check", "--root", str(Path("/fixed/reader"))])
        self.assertEqual(kwargs["input"], data)
        self.assertEqual(set(kwargs["env"]), {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE"})
        self.assertLess(kwargs["timeout"], 180)

    def test_checker_result_cannot_claim_writes_or_invalid_success(self):
        data = b'{"message_ts":"1800000000.000001","file_id":"F123","notice_manifest":{}}'
        for code, value in ((0, notice_socket.NEGATIVE), (1, {**notice_socket.NEGATIVE, "messages_sent": 1}), (2, notice_socket.NEGATIVE)):
            with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, json.dumps(value).encode())):
                with self.assertRaises(ValueError):
                    notice_socket.invoke(data, Path("/fixed/reader"))


@unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux kernel peer credentials required")
class LocalSocketTests(unittest.TestCase):
    def exchange(self, data, allowed_uid=None, checker=None):
        left, right = socket.socketpair()
        calls = []
        def check(raw, root):
            calls.append((raw, root))
            return (checker or (lambda raw, root: notice_socket.NEGATIVE))(raw, root)
        def worker():
            with right:
                notice_socket.handle(right, os.getuid() if allowed_uid is None else allowed_uid, Path("/fixed/reader"), check)
        thread = threading.Thread(target=worker)
        thread.start()
        with left:
            left.settimeout(3)
            try:
                left.sendall(data)
                left.shutdown(socket.SHUT_WR)
                result = left.recv(20000)
            except (ConnectionResetError, BrokenPipeError):
                result = b""
        thread.join(3)
        self.assertFalse(thread.is_alive())
        return result, calls

    def test_peer_identity_rejected_before_invocation(self):
        result, calls = self.exchange(b"private input", allowed_uid=os.getuid() + 1)
        self.assertEqual(result, b"")
        self.assertEqual(calls, [])

    def test_oversized_or_empty_request_cannot_invoke_reader(self):
        for request in (b"", b"x" * (notice_socket.MAX_REQUEST + 1)):
            result, calls = self.exchange(request)
            self.assertEqual(json.loads(result), notice_socket.NEGATIVE)
            self.assertEqual(calls, [])

    def test_reader_error_is_sanitized_without_retry(self):
        def broken(*args):
            raise RuntimeError("secret detail")
        result, calls = self.exchange(b"request", checker=broken)
        self.assertNotIn(b"secret", result)
        self.assertEqual(json.loads(result), notice_socket.NEGATIVE)
        self.assertEqual(len(calls), 1)

    def test_client_round_trip_and_kernel_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o711)
            path = parent / "check.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                path.chmod(0o666)
                listener.listen(1)
                calls = []
                def worker():
                    connection, _ = listener.accept()
                    with connection:
                        notice_socket.handle(connection, os.getuid(), Path("/fixed/reader"),
                                             lambda data, root: calls.append(data) or notice_socket.NEGATIVE)
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertEqual(notice_client.query(path, os.getuid(), b"bounded request"), notice_socket.NEGATIVE)
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(calls, [b"bounded request"])

    def test_client_rejects_unsafe_endpoint_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o711)
            path = parent / "check.sock"
            path.write_bytes(b"not a socket")
            with self.assertRaises(ValueError):
                notice_client.query(path, os.getuid(), b"request")
            path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                path.chmod(0o666)
                parent.chmod(0o777)
                with self.assertRaises(ValueError):
                    notice_client.query(path, os.getuid(), b"request")


if __name__ == "__main__":
    unittest.main()
