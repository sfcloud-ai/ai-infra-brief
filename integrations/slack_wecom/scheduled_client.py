"""固定本机调度客户端，不读取凭据，不接受任意网址或 shell。"""
import argparse
import json
import math
from pathlib import Path
import socket
import stat
import re
import struct
import sys
import time

MAX_REQUEST, MAX_RESULT = 12*1024*1024, 16384


def read_result(data, request):
    """只读快照必须完整且绑定原查询，不能把旧 not_found 当作空日证明。"""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_scheduler_result")
            value[key] = item
        return value
    def require(condition):
        if not condition:
            raise ValueError("invalid_scheduler_result")
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    sent = json.loads(request.decode("utf-8"), object_pairs_hook=unique)
    require(isinstance(result, dict) and isinstance(sent, dict))
    if sent.get("action") != "inspect":
        require(result.get("status") in {"not_found", "not_verified", "queued", "sending", "sent", "failed", "unknown", "partial", "skipped"})
        return result
    if result.get("status") == "not_verified":
        require(result == {"status": "not_verified", "error": "scheduled_request_rejected"})
        return result
    require(set(result) == {"read_context", "observed_at", "complete", "scheduler_day_claim", "scheduler"}
            and result["complete"] is True and type(result["observed_at"]) is int
            and 0 < result["observed_at"] <= 9007199254740991)
    context = result["read_context"]
    require(isinstance(context, dict) and set(context) == {"scheduler_scope_sha256"}
            and isinstance(context["scheduler_scope_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", context["scheduler_scope_sha256"]) is not None)
    claim, scheduler = result["scheduler_day_claim"], result["scheduler"]
    if claim is None or scheduler is None:
        require(claim is None and scheduler is None)
        return result
    day = sent.get("report_date", "")
    canonical_day = day.replace("-", "") if isinstance(day, str) else ""
    successor = (sent.get("request_id") == f"morning-{canonical_day}-r1" and isinstance(claim, dict)
                 and re.fullmatch(f"morning-{canonical_day}-r[1-9][0-9]*", claim.get("request_id", "")))
    require(isinstance(claim, dict) and set(claim) == {"request_id", "report_date", "bundle_sha256", "report_commit"}
            and (claim["request_id"] == sent.get("request_id") or successor) and claim["report_date"] == sent.get("report_date")
            and isinstance(claim["bundle_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", claim["bundle_sha256"]) is not None
            and isinstance(claim["report_commit"], str) and re.fullmatch(r"[a-f0-9]{40}", claim["report_commit"]) is not None)
    fields = {"status", "parts_total", "parts_sent", "phase", "next_attempt_at", "attempts", "last_error", "images"}
    require(isinstance(scheduler, dict) and set(scheduler) == set(claim) | fields
            and all(scheduler[k] == v for k, v in claim.items())
            and scheduler["status"] in {"queued", "sending", "sent", "failed", "unknown"}
            and scheduler["phase"] in {"pending", "verifying", "sending", "unknown", "complete", "superseded"}
            and type(scheduler["parts_total"]) is int and 2 <= scheduler["parts_total"] <= 41
            and type(scheduler["parts_sent"]) is int and 0 <= scheduler["parts_sent"] <= scheduler["parts_total"]
            and type(scheduler["attempts"]) is int and 0 <= scheduler["attempts"] <= 9007199254740991
            and type(scheduler["next_attempt_at"]) in (int, float)
            and math.isfinite(scheduler["next_attempt_at"]) and scheduler["next_attempt_at"] > 0)
    error, images = scheduler["last_error"], scheduler["images"]
    require(error is None or isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", error) is not None)
    require(isinstance(images, list) and len(images) == 1 and isinstance(images[0], dict)
            and set(images[0]) == {"file_id", "status", "error"} and images[0]["file_id"] == "scheduled-poster"
            and images[0]["status"] in {"pending", "sending", "sent", "failed", "unknown"} and images[0]["error"] is None)
    require((scheduler["status"] == "sent") == (scheduler["phase"] == "complete")
            == (scheduler["parts_sent"] == scheduler["parts_total"]))
    require(scheduler["status"] != "sent" or images[0]["status"] == "sent")
    return result


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
    return read_result(b"".join(parts), request)


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
    return 1 if result.get("status") == "not_verified" else 0


if __name__ == "__main__":
    raise SystemExit(main())
