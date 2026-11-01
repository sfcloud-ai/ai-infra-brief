"""固定校验器启动边界：仅切换已安装 release，在原非 root 身份下运行。"""

import errno
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

if __package__:
    from .release import write_json
else:
    from release import write_json

CONFIG = Path("/etc/ai-brief-notice-check.json")
RECORD = Path("/opt/ai-brief-notice-check/release.json")
SERVICE = "ai-brief-notice-check.service"
LABEL = "ai-brief.notice-reader"


def admin_file(path):
    """配置和切换记录只信任管理员所有的普通文件。"""
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("unsafe_notice_admin_file")
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError("unsafe_notice_admin_parent")


def load_config():
    admin_file(CONFIG)
    config = json.loads(CONFIG.read_text())
    if set(config) != {"root", "relay_root", "ipc", "image", "uid", "gid", "publisher_uid", "name"}:
        raise ValueError("invalid_notice_service_config")
    if (any(type(config[k]) is not int or config[k] <= 0 for k in ("uid", "gid", "publisher_uid"))
            or config["uid"] == config["publisher_uid"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"])
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}", config["name"])):
        raise ValueError("invalid_notice_service_identity")
    for key in ("root", "relay_root", "ipc"):
        path = Path(config[key])
        if not path.is_absolute() or path.resolve() != path or "," in str(path):
            raise ValueError("unsafe_notice_service_path")
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config["uid"]
                or stat.S_IMODE(info.st_mode) != (0o711 if key == "ipc" else 0o700)):
            raise ValueError("unsafe_notice_service_directory")
    return config


def command(arguments, timeout=30):
    return subprocess.check_output(arguments, timeout=timeout,
                                   env={"PATH": "/usr/bin:/bin", "HOME": "/root"}).decode().strip()


def release_directory(config, release):
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("invalid_notice_release")
    directory = Path(config["root"]) / "releases" / release
    ready = directory / "ready.json"
    if (directory.resolve() != directory or not ready.is_file() or ready.is_symlink()
            or json.loads(ready.read_text()).get("commit") != release):
        raise ValueError("notice_release_not_ready")
    return directory


def inspect(config, release):
    ids = command(["/usr/bin/docker", "ps", "-aq", "--no-trunc", "--filter",
                   "name=^/" + re.escape(config["name"]) + "$"]).splitlines()
    if not ids:
        return None
    if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
        raise ValueError("unexpected_notice_inventory")
    item = json.loads(command(["/usr/bin/docker", "inspect", ids[0]]))[0]
    if (item["Name"] != "/" + config["name"] or item["Config"].get("Labels", {}).get(LABEL) != release
            or item["Image"] != config["image"]
            or item["Config"].get("User") != str(config["uid"]) + ":" + str(config["gid"])):
        raise ValueError("notice_container_identity_conflict")
    return item


def binding(config, release):
    """只删除确认无监听、无容器且 inode 未变化的本服务残留 socket。"""
    if inspect(config, release) is not None:
        raise ValueError("notice_container_requires_reconciliation")
    directory = Path(config["ipc"])
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != config["uid"]
            or info.st_gid != config["gid"] or stat.S_IMODE(info.st_mode) != 0o711):
        raise ValueError("unsafe_notice_socket_directory")
    endpoint = directory / "check.sock"
    if endpoint.exists() or endpoint.is_symlink():
        old = endpoint.lstat()
        if (not stat.S_ISSOCK(old.st_mode) or old.st_uid != config["uid"]
                or stat.S_IMODE(old.st_mode) != 0o666):
            raise ValueError("unsafe_notice_socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            try:
                client.connect(str(endpoint))
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise ValueError("notice_socket_absence_unconfirmed") from error
            else:
                raise ValueError("notice_listener_already_exists")
        current = endpoint.lstat()
        if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
            raise ValueError("notice_socket_changed")
        endpoint.unlink()


def container_command(config, release, preflight=False):
    directory = release_directory(config, release)
    uid, gid = str(config["uid"]), str(config["gid"])
    target = "/prototype/releases/" + release
    args = ["/usr/bin/docker", "run", "--rm", "--pull", "never", "--network",
            "none" if preflight else "bridge", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--user", uid + ":" + gid,
            "--cpus", "1", "--memory", "256m", "--pids-limit", "32",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,uid=" + uid + ",gid=" + gid,
            "--mount", "type=bind,src=" + str(directory) + ",dst=" + target + ",readonly"]
    if not preflight:
        args += ["--name", config["name"], "--label", LABEL + "=" + release,
                 "--mount", "type=bind,src=" + config["relay_root"] + ",dst=/relay,readonly",
                 "--mount", "type=bind,src=" + config["ipc"] + ",dst=/ipc"]
    args += ["--entrypoint", target + "/venv/bin/python", config["image"], "-I", "-B"]
    if preflight:
        return args + ["-c", "from ai_brief_relay import notice_socket,notice_check; "
                      "assert callable(notice_socket.serve) and callable(notice_check.main)"]
    return args + ["-m", "ai_brief_relay.notice_socket", "--socket", "/ipc/check.sock",
                   "--publisher-uid", str(config["publisher_uid"]), "--root", "/relay"]


def recorded_release():
    admin_file(RECORD)
    record = json.loads(RECORD.read_text())
    if set(record) != {"release"} or not re.fullmatch(r"[0-9a-f]{40}", record["release"]):
        raise ValueError("invalid_notice_release_record")
    return record["release"]


def ready(config, release):
    item = inspect(config, release)
    if not item or not item["State"]["Running"]:
        return False
    host = item["HostConfig"]
    mounts = {mount["Destination"]: mount for mount in item["Mounts"]}
    target = "/prototype/releases/" + release
    expected = {target: (str(release_directory(config, release)), False),
                "/relay": (config["relay_root"], False), "/ipc": (config["ipc"], True)}
    if (not host["ReadonlyRootfs"] or host["CapDrop"] != ["ALL"]
            or host["SecurityOpt"] != ["no-new-privileges"] or host["PortBindings"]
            or host.get("GroupAdd") or host["Privileged"] or host["UsernsMode"] != ""
            or any(path not in mounts or (mounts[path]["Source"], mounts[path]["RW"]) != value
                   for path, value in expected.items())):
        raise ValueError("notice_runtime_isolation_changed")
    endpoint = Path(config["ipc"]) / "check.sock"
    info = endpoint.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != config["uid"]
            or stat.S_IMODE(info.st_mode) != 0o666):
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(endpoint))
        return struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] == config["uid"]


def preflight_container(config, release):
    """超时只会终止 Docker 客户端，因此始终清理本次独有的预检容器。"""
    name = "ai-brief-notice-preflight-" + uuid.uuid4().hex
    arguments = container_command(config, release, preflight=True)
    arguments[3:3] = ["--name", name, "--label", LABEL + "=" + release]
    try:
        command(arguments, timeout=60)
    finally:
        owned = dict(config, name=name)
        item = inspect(owned, release)
        if item is not None:
            command(["/usr/bin/docker", "rm", "-f", item["Id"]], timeout=30)


def preflight(release):
    """停旧实例前，在无凭据、无网络容器里检查候选校验器可导入。"""
    preflight_container(load_config(), release)


def switch(release):
    """在现有部署锁内切换；失败交由原 Supervisor 恢复旧 release，不回写账本。"""
    config = load_config()
    preflight_container(config, release)
    before = recorded_release()
    if before == release:
        try:
            if ready(config, release):
                return
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
    inspect(config, before)
    command(["/bin/systemctl", "stop", SERVICE], timeout=330)
    if inspect(config, before) is not None:
        raise ValueError("notice_stop_unconfirmed")
    write_json(RECORD, {"release": release})
    os.chmod(RECORD, 0o600)
    command(["/bin/systemctl", "start", SERVICE], timeout=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if ready(config, release):
                return
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(0.25)
    raise ValueError("notice_start_health_failed")


def main():
    if os.getuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop"):
        raise ValueError("fixed_notice_service_only")
    config, release = load_config(), recorded_release()
    if sys.argv[1] == "start":
        binding(config, release)
        args = container_command(config, release)
        os.execv(args[0], args)
    else:
        item = inspect(config, release)
        if item:
            command(["/usr/bin/docker", "stop", "--time", "165", item["Id"]], timeout=180)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Notice service failed; reconcile the fixed deployment record.", file=sys.stderr)
        raise SystemExit(1)
