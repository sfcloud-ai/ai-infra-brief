"""专用 SSH key 的 forced-command 候选，不提供交互 shell 或任意命令。"""

import os
import re
import subprocess
import sys


def parse_command(command):
    if command == "status":
        return ("status",)
    match = re.fullmatch(r"(deploy) ([1-9][0-9]{0,17}) ([0-9a-f]{40}) ([0-9a-f]{64})", command)
    if not match:
        match = re.fullmatch(r"(rollback) ([1-9][0-9]{0,17}) ([0-9a-f]{40}) ([0-9a-f]{40})", command)
    if not match:
        raise ValueError("unsupported_deployment_command")
    return match.groups()


if __name__ == "__main__":
    try:
        command = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
        result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/local/lib/ai-brief-control/stage_helper.py", *command],
                                stdin=sys.stdin.buffer, timeout=900, env={"PATH": "/usr/bin:/bin"})
        raise SystemExit(result.returncode)
    except (ValueError, OSError, subprocess.SubprocessError):
        print("restricted_deployment_rejected", file=sys.stderr)
        raise SystemExit(1)
