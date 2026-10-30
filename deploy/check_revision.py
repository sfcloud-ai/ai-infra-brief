"""拒绝被新部署输入替代的版本，允许其后只有文档或 Prompt 变化。"""

import re
import subprocess
import sys


DEPLOY_PATHS = (
    ":(glob)**/*.py", ":(glob)deploy/*.toml", ":(glob)deploy/*.lock",
    ".github/workflows/cd.yml", ".github/workflows/rollback.yml",
    ":(glob)requirements*.lock", "pyproject.toml", "LICENSE",
)


def same_deployment_inputs(revision, current, repository=None):
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (revision, current)):
        raise ValueError("invalid_revision")
    result = subprocess.run(["git", "diff", "--quiet", revision, current, "--", *DEPLOY_PATHS],
                            cwd=repository, capture_output=True, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError("revision_comparison_failed")
    return result.returncode == 0


if __name__ == "__main__":
    revision = sys.argv[1]
    subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", "main"], check=True, timeout=60)
    current = subprocess.check_output(["git", "rev-parse", "FETCH_HEAD"], text=True, timeout=10).strip()
    if not same_deployment_inputs(revision, current):
        raise ValueError("superseded_deployment_inputs")
