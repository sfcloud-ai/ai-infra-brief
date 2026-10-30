#!/usr/bin/python3
"""管理员安装的固定 Docker 暂存 helper；不允许操作线上 relay。"""

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

CONFIG = Path("/etc/ai-brief-stage.json")
CONTROLLER = Path("/usr/local/libexec/ai-brief-release.py")


def root_owned_file(path):
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("unsafe_admin_file")


def docker_command(config, commit, checksum):
    if set(config) != {"root", "uid", "gid", "image"}:
        raise ValueError("invalid_stage_config")
    if (type(config["uid"]) is not int or type(config["gid"]) is not int
            or config["uid"] <= 0 or config["gid"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"])
            or config["root"] != "/opt/ai-infra-brief-prototype"
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
        raise ValueError("invalid_stage_config_or_identity")
    uid, gid = str(config["uid"]), str(config["gid"])
    return ["/usr/bin/docker", "run", "--rm", "--pull", "never", "-i", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", uid + ":" + gid, "--cpus", "1", "--memory", "768m", "--pids-limit", "128",
            "--ipc", "private", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,uid=" + uid + ",gid=" + gid,
            "--mount", "type=bind,src=" + config["root"] + ",dst=/prototype",
            "--mount", "type=bind,src=" + str(CONTROLLER) + ",dst=/controller.py,readonly",
            config["image"], "python", "/controller.py", "--root", "/prototype",
            "--python", "/usr/local/bin/python3.12", "deploy", commit, checksum]


def main():
    if os.getuid() != 0 or len(sys.argv) != 3:
        raise ValueError("restricted_helper_only")
    root_owned_file(CONFIG)
    root_owned_file(CONTROLLER)
    config = json.loads(CONFIG.read_text())
    command = docker_command(config, sys.argv[1], sys.argv[2])
    root = Path(config["root"])
    info = root.lstat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != config["uid"] or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("unsafe_stage_root")
    # 不绑定 Docker socket、用户目录或生产配置；只允许固定的原型目录。
    name = "ai-brief-stage-" + uuid.uuid4().hex
    command[2:2] = ["--name", name]
    try:
        result = subprocess.run(command, stdin=sys.stdin.buffer, timeout=570,
                                env={"PATH": "/usr/bin:/bin", "HOME": "/root"})
        return result.returncode
    finally:
        # 客户端超时不一定停止容器，只清理本次随机命名的暂存容器。
        subprocess.run(["/usr/bin/docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, env={"PATH": "/usr/bin:/bin"})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError):
        print("isolated_stage_helper_failed", file=sys.stderr)
        raise SystemExit(1)
