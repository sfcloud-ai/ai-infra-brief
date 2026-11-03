"""显式启用的定时适配器，复用既有独占锁及串行转发循环。

不提供独立发送入口；调用方传入既有 Store 与 WeCom 对象，且只在活动状态调度。
"""
import datetime as dt
import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import sys

if __package__:
    from . import relay
    from .scheduled_delivery import ScheduledDelivery, canonical, validate_plan
    from .scheduled_socket import handle
    from .scheduled_verifier import NodePublicationVerifier
else:
    import relay
    from scheduled_delivery import ScheduledDelivery, canonical, validate_plan
    from scheduled_socket import handle
    from scheduled_verifier import NodePublicationVerifier


FIELDS = {"activation_date", "publisher_uid", "socket_path", "node_path", "verifier_path"}


def trusted_path(value, *, directory=False, executable=False):
    """拒绝链接，以及可由不受信账号修改的文件和祖先目录。"""
    if not isinstance(value, str):
        raise ValueError("absolute_scheduler_path_required")
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("absolute_scheduler_path_required")
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(info.st_mode) & 0o022
            or (not directory and info.st_nlink != 1)
            or (executable and not os.access(path, os.X_OK))):
        raise ValueError("unsafe_scheduler_path")
    for parent in path.parents:
        item = parent.lstat()
        # root 所有且带粘滞位的 /tmp 不允许其他账号删除本账号私有目录。
        sticky_root = item.st_uid == 0 and bool(item.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(item.st_mode) or item.st_uid not in {0, os.getuid()}
                or (stat.S_IMODE(item.st_mode) & 0o022 and not sticky_root)):
            raise ValueError("unsafe_scheduler_parent")
    return path


def load_config(path):
    path = trusted_path(str(path))
    with path.open("rb") as stream:
        data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError("bounded_scheduler_config_required")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_scheduler_config_field")
            result[key] = value
        return result

    value = json.loads(data, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("exact_scheduler_config_required")
    if (sys.platform != "linux" or os.getuid() <= 0
            or type(value["publisher_uid"]) is not int
            or value["publisher_uid"] <= 0 or value["publisher_uid"] == os.getuid()):
        raise ValueError("distinct_scheduler_publisher_required")
    if (not isinstance(value["activation_date"], str)
            or dt.date.fromisoformat(value["activation_date"]).isoformat() != value["activation_date"]):
        raise ValueError("explicit_scheduler_activation_date_required")
    trusted_path(value["node_path"], executable=True)
    trusted_path(value["verifier_path"])
    if not isinstance(value["socket_path"], str):
        raise ValueError("invalid_scheduler_socket")
    endpoint = Path(value["socket_path"])
    if (not endpoint.is_absolute() or endpoint.name != "scheduled.sock"
            or len(os.fsencode(endpoint)) >= 108):
        raise ValueError("invalid_scheduler_socket")
    parent = trusted_path(str(endpoint.parent), directory=True)
    info = parent.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o711:
        raise ValueError("unsafe_scheduler_socket_directory")
    return value


def clear_stale_socket(path):
    """仅在既有转发器独占锁内调用；绝不删除仍有监听者的入口。"""
    if not path.exists() and not path.is_symlink():
        return
    before = path.lstat()
    if (not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o666):
        raise ValueError("unsafe_scheduler_socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise ValueError("scheduler_socket_absence_unconfirmed") from error
        else:
            raise ValueError("scheduler_listener_already_exists")
    after = path.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("scheduler_socket_changed")
    path.unlink()


class ScheduledHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            handle(self.request, self.server.publisher_uid, self.server.queue, self.server.can_admit)
        except (OSError, TimeoutError):
            pass


class ScheduledRuntime:
    def __init__(self, config, store, relay_config, wecom):
        self.config = config
        self.path = Path(config["socket_path"])
        self.verifier = NodePublicationVerifier(config["node_path"], config["verifier_path"])
        self.server = None
        self.inode = None
        try:
            self.queue = ScheduledDelivery(store, relay_config, self.verifier, wecom,
                                           activation_date=config["activation_date"])
        except BaseException:
            self.verifier.close()
            raise

    def assert_reconciled(self):
        """仅允许已绑定且永久冻结的定时意图与独立任务共存。

        不修改状态、不重试未知分片；旧路径的未决意图仍阻断整个运行时。
        只有显式调度配置及持久化范围通过验证后，才能使用此检查。
        """
        store = self.queue.store
        unresolved = store.db.execute("""SELECT id FROM requests WHERE status IN ('sending','unknown')
            UNION SELECT request_id FROM parts WHERE status IN ('sending','unknown')
            UNION SELECT request_id FROM image_parts WHERE status IN ('sending','unknown')""").fetchall()
        for (request_id,) in unresolved:
            row = store.db.execute("""SELECT r.mode,r.status,r.intent_hash,q.phase,q.record,q.bundle,q.poster,q.due,q.report_date
                FROM requests r LEFT JOIN scheduled_notices q ON q.request_id=r.id WHERE r.id=?""", (request_id,)).fetchone()
            try:
                if (row is None or row[0] != "scheduled" or row[1] not in ("sending", "unknown")
                        or row[3] not in ("sending", "unknown")):
                    raise ValueError("not_a_frozen_scheduled_intent")
                record = json.loads(row[4])
                checked, _, due = validate_plan({**record, "poster": row[6]}, row[5], record["report_commit"])
                profile = relay.validate_notice_connector(self.queue.config["notice_connector"])
                day = relay.notice_url_day(record["notice_key"], profile)
                edition = relay.digest([self.queue.config["team_id"], self.queue.config["channel_id"], record["notice_key"]])
                day_key = relay.digest([self.queue.config["team_id"], self.queue.config["channel_id"], day])
                if (canonical(checked) != row[4] or relay.digest(checked) != row[2]
                        or record["report_request_id"] != request_id or record["report_date"] != row[8]
                        or day != row[8] or due != row[7]
                        or store.get("notice_edition:" + edition) != request_id
                        or store.get("notice_edition_date:" + edition) != day
                        or store.get("notice_day:" + day_key) != request_id):
                    raise ValueError("frozen_scheduled_binding_differs")
            except Exception as error:
                raise ValueError("unreconciled_delivery_blocks_cutover") from error

    def open(self, can_admit):
        clear_stale_socket(self.path)
        try:
            self.server = socketserver.UnixStreamServer(str(self.path), ScheduledHandler)
            info = self.path.lstat()
            self.inode = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o666)
            self.server.publisher_uid = self.config["publisher_uid"]
            self.server.queue = self.queue
            self.server.can_admit = can_admit
            self.server.timeout = 0
            return self.server
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.server is not None:
                self.server.server_close()
                self.server = None
            if self.inode is not None:
                try:
                    info = self.path.lstat()
                    if (info.st_dev, info.st_ino) == self.inode:
                        self.path.unlink()
                except FileNotFoundError:
                    pass
                self.inode = None
        finally:
            self.verifier.close()
