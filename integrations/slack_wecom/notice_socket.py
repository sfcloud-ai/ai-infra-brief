"""仅限 Linux 的固定用途本机入口，只调用既有只读通知校验器。"""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time

MAX_REQUEST = 65536
MAX_RESULT = 16384
NEGATIVE = {"slack_status": "not_verified", "wecom_status": "not_checked",
            "error": "notice_read_unverified", "messages_sent": 0, "state_changed": False}


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def bounded_read(connection, maximum, seconds):
    until, chunks, size = time.monotonic() + seconds, [], 0
    while True:
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("local_read_deadline")
        connection.settimeout(remaining)
        part = connection.recv(min(4096, maximum + 1 - size))
        if not part:
            return b"".join(chunks)
        chunks.append(part)
        size += len(part)
        if size > maximum:
            raise ValueError("local_request_too_large")


def invoke(data, root):
    # 启动读取进程或接触凭据前先解析请求；校验器再按本机私有配置逐项验证。
    request = json.loads(data.decode("utf-8"))
    if not isinstance(request, dict) or set(request) != {"message_ts", "file_id", "notice_manifest"}:
        raise ValueError("invalid_request")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "ai_brief_relay.notice_check", "--root", str(root)],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=150,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}, check=False)
    if result.returncode not in (0, 1) or not 0 < len(result.stdout) <= MAX_RESULT:
        raise ValueError("checker_unavailable")
    value = json.loads(result.stdout.decode("utf-8"))
    if (not isinstance(value, dict) or value.get("messages_sent") != 0
            or value.get("state_changed") is not False
            or value.get("slack_status") != ("verified" if result.returncode == 0 else "not_verified")):
        raise ValueError("invalid_checker_result")
    return value


def handle(connection, allowed_uid, root, checker=invoke):
    # SO_PEERCRED 来自内核，不能由 JSON 中声明的身份替代。
    # 身份不符时不读取请求、不返回内容，也不调用校验器。
    if peer_uid(connection) != allowed_uid:
        return
    try:
        data = bounded_read(connection, MAX_REQUEST, 10)
        if not data:
            raise ValueError("empty_request")
        value = checker(data, root)
        result = json.dumps(value, ensure_ascii=True).encode("ascii")
        if len(result) > MAX_RESULT:
            raise ValueError("result_too_large")
    except Exception:
        result = json.dumps(NEGATIVE).encode("ascii")
    connection.settimeout(5)
    connection.sendall(result)


def serve(path, allowed_uid, root):
    if (sys.platform != "linux" or os.getuid() <= 0 or type(allowed_uid) is not int or allowed_uid <= 0
            or not path.is_absolute() or path.parent.resolve() != path.parent):
        raise ValueError("invalid_local_binding")
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o711):
        raise ValueError("unsafe_socket_directory")
    # 不自动删除来历不明或残留的入口；监督器须先确认原进程已停止。
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        try:
            os.chmod(path, 0o666)
            listener.listen(4)
            while True:
                connection, _ = listener.accept()
                with connection:
                    try:
                        handle(connection, allowed_uid, root)
                    except (OSError, TimeoutError):
                        pass
        finally:
            path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve only the existing read-only notice checker")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--publisher-uid", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.root.is_absolute() or args.root.resolve() != args.root:
            raise ValueError("absolute_reader_root_required")
        serve(args.socket, args.publisher_uid, args.root)
    except Exception:
        print('{"error":"notice_socket_unavailable"}', file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
