"""构建仅含转发运行代码的独立 wheel；Prompt 由写日报的任务从 GitHub 读取。"""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


RUNTIME_FILES = ("__init__.py", "relay.py", "images.py", "resume.py", "runtime.py")


def build_relay(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-build-") as directory:
        root = Path(directory)
        package = root / "ai_brief_relay"
        package.mkdir()
        inputs = [(source / "integrations/slack_wecom" / name, package / name) for name in RUNTIME_FILES]
        inputs += [(source / "LICENSE", root / "LICENSE"),
                   (source / "deploy/relay-package.toml", root / "pyproject.toml")]
        for origin, destination in inputs:
            if origin.is_symlink() or not origin.is_file():
                raise ValueError("invalid_relay_build_input")
            shutil.copyfile(origin, destination)
        environment = {"PATH": os.defpath, "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1",
                       "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "315532800")}
        result = subprocess.run([sys.executable, "-I", "-B", "-c",
                                 "from setuptools.build_meta import build_wheel; build_wheel(" + repr(str(output)) + ")"],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("relay_wheel_build_failed: " + result.stderr)
    wheels = list(output.glob("sfcloud_ai_relay-*.whl"))
    if len(wheels) != 1:
        raise ValueError("unexpected_relay_wheel_set")
    return wheels[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(build_relay(Path(__file__).resolve().parents[1], args.output))
