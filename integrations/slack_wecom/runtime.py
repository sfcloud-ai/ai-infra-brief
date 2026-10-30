"""单实例受控入口：就绪后暂停投递，显式激活，暂停响应即完成当前工作。"""

import argparse
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
    from .resume import assert_reconciled, binding, resume_class
else:
    from resume import assert_reconciled, binding, resume_class


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
    spec.loader.exec_module(module)
    if Path(sys.modules["images"].__file__).resolve() != directory / "images.py":
        raise ValueError("unexpected_image_module")
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
    def __init__(self, module, receiver, release):
        self.module, self.receiver, self.release = module, receiver, release
        self.phase = "paused"
        self.polls = 0

    def ready(self):
        assert_reconciled(self.receiver.store)
        self.receiver.slack.newer(self.receiver.store.get("cursor"))

    def dispatch(self, path, payload, uid):
        if uid != os.getuid():
            raise self.module.RelayError("forbidden", 403)
        if path.startswith("/control/"):
            if payload != {"release": self.release}:
                raise self.module.RelayError("release_mismatch", 409)
            operation = path.removeprefix("/control/")
            if operation == "pause":
                self.phase = "paused"
            elif operation == "activate":
                self.phase = "paused"
                self.ready()
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
        return self.receiver.dispatch(path, payload, uid)

    def poll(self):
        if self.phase == "active":
            try:
                self.receiver.poll()
                assert_reconciled(self.receiver.store)
                self.polls += 1
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
            controller = ControlledRelay(module, receiver, release)
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
                                controller.poll()
                            except Exception:
                                controller.phase = "paused"
                                print('{"event":"delivery_paused_requires_reconcile"}', flush=True)
                            next_poll = time.monotonic() + config["poll_seconds"]
            finally:
                signal.signal(signal.SIGTERM, previous)
                socket_path.unlink(missing_ok=True)
        finally:
            store.db.close()


def control(module, root, operation, release):
    module.secure_dir(root / "run")
    connection = module.UnixConnection(root / "run/relay.sock")
    try:
        connection.request("POST", "/control/" + operation,
                           body=json.dumps({"release": release}), headers={"Content-Type": "application/json"})
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
        control(module, args.root, args.operation, args.release)
    else:
        serve(module, args.root, args.release, args.activation_file)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"controlled_relay_failed"}', file=sys.stderr)
        raise SystemExit(1)
