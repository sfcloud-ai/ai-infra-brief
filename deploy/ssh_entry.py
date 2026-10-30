"""专用 SSH key 的 forced-command 候选，不提供交互 shell 或任意命令。"""

import os
import re
import subprocess
import sys


def parse_command(command):
    match = re.fullmatch(r"deploy ([0-9a-f]{40}) ([0-9a-f]{64})", command)
    if not match:
        raise ValueError("unsupported_deployment_command")
    return match.groups()


if __name__ == "__main__":
    try:
        commit, checksum = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/local/libexec/ai-brief-stage", commit, checksum],
                                stdin=sys.stdin.buffer, timeout=600, env={"PATH": "/usr/bin:/bin"})
        raise SystemExit(result.returncode)
    except (ValueError, OSError, subprocess.SubprocessError):
        print("restricted_deployment_rejected", file=sys.stderr)
        raise SystemExit(1)
