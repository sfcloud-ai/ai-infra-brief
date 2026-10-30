#!/usr/bin/python3
"""管理员安装的受限部署入口；持续权限及首次生产切换均需显式配置批准。"""

import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

if __package__:
    from .policy import Policy
    from .ssh_entry import parse_command
    from .supervisor import DockerDriver, Supervisor
else:
    from policy import Policy
    from ssh_entry import parse_command
    from supervisor import DockerDriver, Supervisor

CONFIG = Path("/etc/ai-brief-stage.json")
CODE_ROOT = Path("/usr/local/lib/ai-brief-control")
CONTROLLER = CODE_ROOT / "release.py"
CONTROL_ROOT = Path("/var/lib/ai-brief-control")


def root_owned_file(path):
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("unsafe_admin_file")
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("unsafe_admin_parent")


def docker_command(config, commit, checksum, protected=()):
    required = {"root", "uid", "gid", "image"}
    if not required <= set(config) or set(config) - required - {"live_enabled", "bootstrap_approved", "live"}:
        raise ValueError("invalid_stage_config")
    if (type(config["uid"]) is not int or type(config["gid"]) is not int
            or config["uid"] <= 0 or config["gid"] <= 0
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"])
            or config["root"] != "/opt/ai-infra-brief-prototype"
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or type(config.get("live_enabled", False)) is not bool
            or type(config.get("bootstrap_approved", False)) is not bool
            or any(not re.fullmatch(r"[0-9a-f]{40}", item) for item in protected)):
        raise ValueError("invalid_stage_config_or_identity")
    uid, gid = str(config["uid"]), str(config["gid"])
    command = ["/usr/bin/docker", "run", "--rm", "--pull", "never", "-i", "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", uid + ":" + gid, "--cpus", "1", "--memory", "768m", "--pids-limit", "128",
               "--ipc", "private", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,uid=" + uid + ",gid=" + gid,
               "--mount", "type=bind,src=" + config["root"] + ",dst=/prototype",
               "--mount", "type=bind,src=" + str(CONTROLLER) + ",dst=/controller.py,readonly",
               config["image"], "python", "/controller.py", "--root", "/prototype",
               "--python", "/usr/local/bin/python3.12"]
    for commit_to_keep in sorted(protected):
        command += ["--protect", commit_to_keep]
    return command + ["deploy", commit, checksum]


def stage(config, commit, checksum, protected):
    command = docker_command(config, commit, checksum, protected)
    name = "ai-brief-stage-" + uuid.uuid4().hex
    command[2:2] = ["--name", name]
    try:
        result = subprocess.run(command, stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=570, env={"PATH": "/usr/bin:/bin", "HOME": "/root"})
        if result.returncode:
            raise ValueError("isolated_stage_failed")
    finally:
        subprocess.run(["/usr/bin/docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, env={"PATH": "/usr/bin:/bin"})


def execute(command, config, control_root, stage_runner=stage, driver_factory=DockerDriver):
    policy = Policy(control_root)
    if command == ("status",):
        return {"sequence": policy.value["sequence"], "active": policy.value["active"],
                "previous": policy.value["previous"]}
    operation, sequence, first, second = command
    sequence = int(sequence)
    if operation == "deploy":
        policy.accept(sequence, first, second)
        stage_runner(config, first, second, policy.protected())
        if not config.get("live_enabled"):
            return {"status": "staged_only_production_disabled", "commit": first}
        supervisor = Supervisor(control_root, driver_factory(config, control_root), policy)
        supervisor.switch(first)
        return {"status": "activated", "commit": first, "sequence": sequence}
    if not config.get("live_enabled"):
        raise ValueError("production_disabled")
    policy.check_rollback(sequence, first, second)
    supervisor = Supervisor(control_root, driver_factory(config, control_root), policy)
    supervisor.switch(second)
    policy.rolled_back(second)
    return {"status": "rolled_back", "commit": second, "sequence": sequence}


def main():
    if os.getuid() != 0:
        raise ValueError("restricted_helper_only")
    command = parse_command(" ".join(sys.argv[1:]))
    for path in (CONFIG, CONTROLLER, CODE_ROOT / "policy.py", CODE_ROOT / "supervisor.py", CODE_ROOT / "ssh_entry.py"):
        root_owned_file(path)
    config = json.loads(CONFIG.read_text())
    # 即使是 status/rollback，也验证固定配置，不能让未验证路径进入 Docker。
    docker_command(config, "0" * 40, "0" * 64)
    root = Path(config["root"])
    info = root.lstat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != config["uid"] or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("unsafe_stage_root")
    info = CONTROL_ROOT.lstat()
    if (CONTROL_ROOT.resolve() != CONTROL_ROOT or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("unsafe_control_root")
    if config.get("live_enabled"):
        if config["live"]["control_code"] != str(CODE_ROOT / "relay"):
            raise ValueError("unexpected_control_code")
        for name in ("runtime.py", "resume.py"):
            root_owned_file(CODE_ROOT / "relay" / name)
    fd = os.open(CONTROL_ROOT / "deployment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(execute(command, config, CONTROL_ROOT)))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"restricted_deployment_failed"}', file=sys.stderr)
        raise SystemExit(1)
