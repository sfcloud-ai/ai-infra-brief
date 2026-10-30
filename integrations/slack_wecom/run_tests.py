"""仅运行转发器离线测试；审计钩子拒绝互联网套接字、DNS 和子进程。"""

import json
from pathlib import Path
import socket
import sys
import unittest

attempts = []


def guard(event, args):
    denied = event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "subprocess.Popen", "os.system"}
    if event == "socket.__new__" and args[1] != socket.AF_UNIX:
        denied = True
    if event == "socket.connect" and args[0].family != socket.AF_UNIX:
        denied = True
    if denied:
        attempts.append(event)
        raise RuntimeError("offline_test_blocked_external_operation")


def main():
    sys.addaudithook(guard)
    if sys.argv[1:] == ["--self-test-guard"]:
        for operation in (lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                          lambda: socket.getaddrinfo("example.invalid", 443)):
            try:
                operation()
            except RuntimeError as error:
                if str(error) != "offline_test_blocked_external_operation":
                    raise
            else:
                raise AssertionError("offline guard did not block the operation")
        print(json.dumps({"guard_self_test": "passed", "blocked_before_network": len(attempts)}))
        return 0
    strict = sys.argv[1:] == ["--require-unix-socket"]
    if sys.argv[1:] and not strict:
        raise SystemExit("Only --self-test-guard or --require-unix-socket is supported")
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent), pattern="test_*.py")
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.close()
        socket_available = True
    except PermissionError:
        socket_available = False
    if not socket_available and not strict:
        def mark_unavailable(tests):
            for case in tests:
                if isinstance(case, unittest.TestSuite):
                    mark_unavailable(case)
                elif case.id().endswith(".test_unix_http_peer_and_json_no_network"):
                    reason = "当前沙箱禁止 AF_UNIX；保留原测试，待允许本地套接字的环境执行。"
                    setattr(case, "test_unix_http_peer_and_json_no_network", lambda case=case: case.skipTest(reason))
        mark_unavailable(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"tests_run": result.testsRun, "skipped": len(result.skipped), "unix_socket_available": socket_available,
                      "outbound_attempts": len(attempts), "guard": "python_audit"}))
    return 0 if result.wasSuccessful() and not attempts and (not strict or not result.skipped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
