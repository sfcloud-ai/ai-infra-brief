"""单实例受控入口：就绪后暂停投递，显式激活，暂停响应即完成当前工作。"""

import argparse
import math
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socketserver
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace

if __package__:
    from . import resume as resume_adapter
    from .resume import RetryableReadError, assert_reconciled, binding, resume_class
else:
    import resume as resume_adapter
    from resume import RetryableReadError, assert_reconciled, binding, resume_class


def load_module(code=None):
    if code is None:
        if not __package__:
            raise ValueError("code_directory_required")
        from . import relay
        return relay
    directory = Path(code).resolve(strict=True)
    for name in ("relay.py", "images.py"):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid_relay_code")
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("managed_relay", directory / "relay.py")
    module = importlib.util.module_from_spec(spec)
    # 固定控制层主动传入能力；旧控制层不会设置，新应用在预检时拒绝。
    module.CONTROL_CAPABILITIES = getattr(resume_adapter, "CONTROL_CAPABILITIES", frozenset())
    spec.loader.exec_module(module)
    for name in ("images", "notice_format"):
        imported = getattr(module, name, None) if name == "notice_format" else sys.modules.get(name)
        if imported is None and name == "notice_format":
            # 历史 release 与 /legacy 只包含 images，不强迫它们依赖新模块。
            continue
        path = directory / (name + ".py")
        if (imported is None or path.is_symlink() or not path.is_file()
                or Path(imported.__file__).resolve() != path):
            raise ValueError("unexpected_runtime_module")
    return module


def handoff(module, root):
    config = module.load_config(root)
    with module.exclusive(root):
        database = root / "state/state.sqlite3"
        os.close(module.secure_open(database))
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as source:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("state_integrity_failed")
            assert_reconciled(SimpleNamespace(db=source))
            row = source.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
            if row is None or not module.TS.fullmatch(row[0]):
                raise ValueError("handoff_cursor_required")
            existing = source.execute("SELECT value FROM meta WHERE key='handoff_binding_v1'").fetchone()
            if existing and existing[0] != binding(config):
                raise ValueError("handoff_identity_changed")
            if not existing:
                backups = root / "state/handoff-backups"
                backups.mkdir(mode=0o700, exist_ok=True)
                module.secure_dir(backups)
                # 未完成文件不会被当作有效备份；仅清理本机制创建的、同用户文件。
                for path in backups.iterdir():
                    if re.fullmatch(r"before-handoff-\d+\.partial", path.name):
                        os.close(module.secure_open(path))
                        path.unlink()
                target = backups / ("before-handoff-" + str(time.time_ns()) + ".sqlite3")
                partial = target.with_suffix(".partial")
                with sqlite3.connect(partial) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("backup_integrity_failed")
                os.chmod(partial, 0o600)
                with partial.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(partial, target)
                saved = sorted(path for path in backups.iterdir()
                               if re.fullmatch(r"before-handoff-\d+\.sqlite3", path.name))
                for path in saved[:-3]:
                    os.close(module.secure_open(path))
                    path.unlink()
        with sqlite3.connect(database) as target:
            target.execute("INSERT OR REPLACE INTO meta VALUES ('handoff_binding_v1',?)", (binding(config),))
        print('{"handoff_ready":true,"messages_sent":0}')


def check_state(module, root):
    module.load_config(root)
    database = root / "state/state.sqlite3"
    os.close(module.secure_open(database))
    if shutil.disk_usage(root / "state").free < database.stat().st_size * 2 + 64 * 1024 * 1024:
        raise ValueError("handoff_backup_free_space_required")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as source:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("state_integrity_failed")
        assert_reconciled(SimpleNamespace(db=source))
        row = source.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        if row is None or not module.TS.fullmatch(row[0]):
            raise ValueError("handoff_cursor_required")
    print('{"state_reconciled":true,"messages_sent":0}')


class ControlledRelay:
    def __init__(self, module, receiver, release, activation_file=None):
        self.activation_file = activation_file
        self.revoked_nonce = None
        self.module, self.receiver, self.release = module, receiver, release
        self.phase = "paused"
        self.polls = 0

    def ready(self):
        assert_reconciled(self.receiver.store)
        self.receiver.slack.newer(self.receiver.store.get("cursor"))

    def deadline(self, payload):
        deadline = payload.get("deadline")
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or not time.monotonic() < deadline <= time.monotonic() + 1800):
            raise self.module.RelayError("control_request_expired", 409)

    def permit(self):
        path = self.activation_file.with_name("permit.json")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise self.module.RelayError("unsafe_activation_permit", 409)
        return json.loads(path.read_text())

    def authorized(self, payload):
        self.deadline(payload)
        if self.activation_file is not None:
            nonce = payload.get("nonce")
            if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
                    or nonce == self.revoked_nonce
                    or self.permit() != {"release": self.release, "nonce": nonce}):
                raise self.module.RelayError("activation_permit_revoked", 409)

    def dispatch(self, path, payload, uid):
        if uid != os.getuid():
            raise self.module.RelayError("forbidden", 403)
        if path.startswith("/control/"):
            if (not isinstance(payload, dict) or set(payload) != {"release", "deadline", "nonce"}
                    or payload["release"] != self.release):
                raise self.module.RelayError("release_mismatch", 409)
            # 在任何状态变更前拒绝过期请求，避免失去客户端的旧操作迟到生效。
            self.deadline(payload)
            operation = path.removeprefix("/control/")
            if operation == "pause":
                self.phase = "paused"
                if self.activation_file is not None:
                    self.revoked_nonce = self.permit().get("nonce")
            elif operation == "activate":
                self.authorized(payload)
                self.phase = "paused"
                self.ready()
                # readiness 可以等待网络；返回后再次确认调用期限和 root 授权。
                self.authorized(payload)
                self.polls = 0
                self.phase = "active"
            elif operation != "status":
                raise self.module.RelayError("not_found", 404)
            # 同一线程执行请求和 poll，此响应说明当前没有正在执行的投递调用。
            try:
                assert_reconciled(self.receiver.store)
                reconciled = True
            except ValueError:
                reconciled = False
            return {"release": self.release, "phase": self.phase, "in_flight": False,
                    "reconciled": reconciled, "successful_polls": self.polls}
        if self.phase != "active" and path != "/status":
            raise self.module.RelayError("delivery_paused", 409)
        if path == "/status":
            return self.receiver.dispatch(path, payload, uid)
        try:
            assert_reconciled(self.receiver.store)
            return self.receiver.dispatch(path, payload, uid)
        finally:
            # 手动投递也共享账本；未知回执不能等到下一次轮询才阻断发送。
            try:
                assert_reconciled(self.receiver.store)
            except Exception:
                self.phase = "paused"

    def poll(self):
        if self.phase == "active":
            try:
                assert_reconciled(self.receiver.store)
                processed = self.receiver.poll()
                assert_reconciled(self.receiver.store)
                self.polls += 1
                return processed
            except RetryableReadError:
                self.polls = 0
                raise
            except Exception:
                self.phase = "paused"
                raise


def serve(module, root, release, activation_file=None):
    config = module.load_config(root)
    with module.exclusive(root):
        credentials = module.load_secrets(root)
        slack = module.Slack(credentials["slack_user_token"], config)
        slack.verify()
        store = module.Store(root / "state")
        try:
            assert_reconciled(store)
            receiver = resume_class(module)(store, slack, module.WeCom(credentials["wecom_webhook"]), config)
            receiver.baseline()
            controller = ControlledRelay(module, receiver, release, activation_file)
            controller.ready()
            if activation_file and activation_file.exists():
                info = activation_file.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                        or stat.S_IMODE(info.st_mode) & 0o022):
                    raise ValueError("unsafe_activation_record")
                if json.loads(activation_file.read_text()) == {"release": release}:
                    controller.phase = "active"
            socket_path = root / "run/relay.sock"
            if socket_path.exists() or socket_path.is_symlink():
                info = socket_path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("unsafe_socket")
                socket_path.unlink()
            stopping = False

            def stop(signum, frame):
                nonlocal stopping
                stopping = True

            previous = signal.signal(signal.SIGTERM, stop)
            try:
                with socketserver.UnixStreamServer(str(socket_path), module.Handler) as server:
                    os.chmod(socket_path, 0o600)
                    server.relay, server.timeout = controller, 0.25
                    next_poll = time.monotonic()
                    while not stopping:
                        server.handle_request()
                        if not stopping and controller.phase == "active" and time.monotonic() >= next_poll:
                            try:
                                processed = controller.poll()
                            except RetryableReadError as error:
                                event = "notice_read_retry" if str(error) == "notice_read_retry" else "history_read_retry"
                                print(json.dumps({"event": event}), flush=True)
                                next_poll = time.monotonic() + min(config["poll_seconds"], 30)
                            except Exception:
                                controller.phase = "paused"
                                print('{"event":"delivery_paused_requires_reconcile"}', flush=True)
                            else:
                                next_poll = time.monotonic() + (0 if processed else config["poll_seconds"])
            finally:
                signal.signal(signal.SIGTERM, previous)
                socket_path.unlink(missing_ok=True)
        finally:
            store.db.close()


def control(module, root, operation, release, deadline, nonce=None):
    module.secure_dir(root / "run")
    if deadline is None or not math.isfinite(deadline) or not time.monotonic() < deadline <= time.monotonic() + 1800:
        raise ValueError("control_request_expired")
    connection = module.UnixConnection(root / "run/relay.sock")
    connection.timeout = deadline - time.monotonic()
    try:
        connection.request("POST", "/control/" + operation,
                           body=json.dumps({"release": release, "deadline": deadline, "nonce": nonce}), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read(8192))
        if response.status != 200 or result.get("release") != release:
            raise ValueError("control_rejected")
        print(json.dumps(result))
    finally:
        connection.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capabilities", "check-state", "handoff", "serve", "control"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--operation", choices=("status", "pause", "activate"))
    parser.add_argument("--activation-file", type=Path)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--activation-nonce")
    args = parser.parse_args()
    module = load_module(args.code)
    if args.command == "capabilities":
        print('{"relay":true,"resume":true,"pause_activate":true,"messages_sent":0}')
        return
    if args.root is None or not args.root.is_absolute():
        raise ValueError("absolute_runtime_root_required")
    if args.command == "handoff":
        handoff(module, args.root)
        return
    if args.command == "check-state":
        check_state(module, args.root)
        return
    if not args.release or not re.fullmatch(r"[0-9a-f]{40}", args.release):
        raise ValueError("release_required")
    if args.command == "control":
        if not args.operation:
            raise ValueError("operation_required")
        control(module, args.root, args.operation, args.release, args.deadline, args.activation_nonce)
    else:
        serve(module, args.root, args.release, args.activation_file)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"controlled_relay_failed"}', file=sys.stderr)
        raise SystemExit(1)
