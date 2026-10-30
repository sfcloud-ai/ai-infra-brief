"""使用临时密钥文件和固定主机信任，将包送给受限 SSH 命令。"""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def ssh_arguments(host, user, port, key, known_hosts, commit, checksum):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", host):
        raise ValueError("invalid_host")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("invalid_user")
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid_port")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("invalid_release_identity")
    return ["ssh", "-F", "/dev/null", "-T", "-i", str(key), "-p", port,
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            user + "@" + host, "deploy " + commit + " " + checksum]


def main():
    if (os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_REPOSITORY") != "sfcloud-ai/ai-infra-brief"):
        raise ValueError("untrusted_trigger")
    path = Path(sys.argv[1])
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
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
        command = ssh_arguments(os.environ["T4_SSH_HOST"], os.environ["T4_SSH_USER"],
                                os.environ["T4_SSH_PORT"], key, known, os.environ["GITHUB_SHA"], checksum)
        # 子进程不继承私钥文本；SSH 错误可能含私有地址，不能回显到公开日志。
        environment = {name: value for name, value in os.environ.items() if not name.startswith("T4_")}
        with path.open("rb") as stream:
            result = subprocess.run(command, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=600, env=environment)
        if result.returncode:
            raise RuntimeError("ssh_deploy_failed_check_private_host_audit")
    print("prototype_release_success")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("prototype_release_failed_no_private_details_logged", file=sys.stderr)
        raise SystemExit(1)
