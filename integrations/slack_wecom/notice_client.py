"""独立 Python 3.8+ 客户端；不读取凭据，也不导入转发器。"""
import argparse
import json
from pathlib import Path
import socket
import stat
import struct
import sys
import time

MAX_REQUEST, MAX_RESULT = 65536, 16384


def query(path, reader_uid, request):
    if (sys.platform != "linux" or type(reader_uid) is not int or reader_uid <= 0
            or not isinstance(request, bytes) or not 0 < len(request) <= MAX_REQUEST
            or not path.is_absolute() or path.parent.resolve() != path.parent):
        raise ValueError("invalid_local_binding")
    parent, endpoint = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != reader_uid
            or stat.S_IMODE(parent.st_mode) != 0o711
            or not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != reader_uid
            or stat.S_IMODE(endpoint.st_mode) != 0o666):
        raise ValueError("unsafe_local_endpoint")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        observed_uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if observed_uid != reader_uid:
            raise ValueError("unexpected_reader_identity")
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        until, parts, size = time.monotonic() + 165, [], 0
        while True:
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("checker_deadline")
            connection.settimeout(remaining)
            part = connection.recv(min(4096, MAX_RESULT + 1 - size))
            if not part:
                break
            parts.append(part)
            size += len(part)
            if size > MAX_RESULT:
                raise ValueError("checker_result_too_large")
    result = json.loads(b"".join(parts).decode("utf-8"))
    if (not isinstance(result, dict) or result.get("slack_status") not in ("verified", "not_verified")
            or result.get("messages_sent") != 0 or result.get("state_changed") is not False):
        raise ValueError("invalid_checker_result")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Call only a pinned local read-only notice reader")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--reader-uid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = query(args.socket, args.reader_uid, sys.stdin.buffer.read(MAX_REQUEST + 1))
    except Exception:
        result = {"slack_status": "not_verified", "wecom_status": "not_checked",
                  "error": "notice_read_unverified", "messages_sent": 0, "state_changed": False}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["slack_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
