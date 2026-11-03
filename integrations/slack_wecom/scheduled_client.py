"""固定本机调度客户端，不读取凭据，不接受任意网址或 shell。"""
import argparse
import json
from pathlib import Path
import socket
import stat
import struct
import sys
import time

MAX_REQUEST, MAX_RESULT = 12*1024*1024, 16384


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
    if not isinstance(result, dict) or result.get("status") not in {
        "not_found","not_verified","queued","sending","sent","failed","unknown","partial"}:
        raise ValueError("invalid_scheduler_result")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Call only a pinned relay-owned scheduling endpoint")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--reader-uid", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = query(args.socket, args.reader_uid, sys.stdin.buffer.read(MAX_REQUEST + 1))
    except Exception:
        result = {"status":"not_verified","error":"scheduled_endpoint_unavailable"}
    print(json.dumps(result, ensure_ascii=True))
    return 1 if result["status"] == "not_verified" else 0


if __name__ == "__main__":
    raise SystemExit(main())
