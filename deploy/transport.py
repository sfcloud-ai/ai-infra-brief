"""使用临时密钥文件和固定主机信任，将包送给受限 SSH 命令。"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if __package__:
    from .ssh_entry import parse_command
else:
    from ssh_entry import parse_command


class ReceiverUpgradeRequired(RuntimeError):
    pass


def ssh_arguments(host, user, port, key, known_hosts, commit, checksum, sequence=1, command=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("invalid_host")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("invalid_user")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid_port")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("invalid_release_identity")
    remote = command or "deploy " + str(sequence) + " " + commit + " " + checksum
    parse_command(remote)
    return ["ssh", "-F", "/dev/null", "-T", "-i", str(key), "-p", port,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            user + "@" + host, remote]


def main():
    rollback = len(sys.argv) == 3 and sys.argv[1] == "--rollback"
    if (os.environ.get("GITHUB_EVENT_NAME") != ("workflow_dispatch" if rollback else "push") or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY") != "sfcloud-ai/ai-infra-brief"):
        raise ValueError("untrusted_trigger")
    path = None if rollback else Path(sys.argv[1])
    checksum = "0" * 64 if rollback else hashlib.sha256(path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ai-brief-ssh-", dir=os.environ["RUNNER_TEMP"]) as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        key, known = root / "identity", root / "known_hosts"
        for target, name in ((key, "T4_SSH_PRIVATE_KEY"), (known, "T4_SSH_KNOWN_HOSTS")):
            value = os.environ[name]
            if not value.strip():
                raise ValueError("missing_ssh_material")
            target.write_text(value.rstrip() + "\n", encoding="utf-8")
            os.chmod(target, 0o600)
        # 子进程不继承私钥文本；SSH 错误可能含私有地址，不能回显到公开日志。
        environment = {name: value for name, value in os.environ.items() if not name.startswith("T4_")}

        def invoke(remote=None, stream=None, timeout=10920):
            command = ssh_arguments(os.environ["T4_SSH_HOST"], os.environ["T4_SSH_USER"],
                                    os.environ["T4_SSH_PORT"], key, known, os.environ["GITHUB_SHA"], checksum,
                                    int(os.environ["GITHUB_RUN_NUMBER"]), remote)
            result = subprocess.run(command, stdin=stream or subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, env=environment)
            if result.returncode:
                raise RuntimeError("ssh_operation_failed_check_private_host_audit")
            return json.loads(result.stdout)

        if rollback:
            target = sys.argv[2]
            if not re.fullmatch(r"[0-9a-f]{40}", target):
                raise ValueError("invalid_rollback_target")
            status = invoke("status")
            result = invoke("rollback " + str(status["sequence"]) + " " + str(status["active"]) + " " + target)
            if result.get("status") != "rolled_back" or result.get("commit") != target:
                raise RuntimeError("rollback_not_confirmed")
        else:
            status = invoke("status", timeout=60)
            if not isinstance(status, dict) or status.get("receiver_format") != "relay-only-v1":
                raise ReceiverUpgradeRequired("administrator_receiver_upgrade_required")
            with path.open("rb") as stream:
                result = invoke(stream=stream)
            if result.get("status") != "activated" or result.get("commit") != os.environ["GITHUB_SHA"]:
                raise RuntimeError("production_activation_not_confirmed")
    print("controlled_release_success")


if __name__ == "__main__":
    try:
        main()
    except ReceiverUpgradeRequired:
        print("administrator_receiver_upgrade_required", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("prototype_release_failed_no_private_details_logged", file=sys.stderr)
        raise SystemExit(1)
