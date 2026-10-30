"""在隔离目录实际安装 wheel，验证发布、幂等和回滚；不接触 relay。"""

import argparse
import os
from pathlib import Path
import sys
import signal
import subprocess
import time

from bundle import make_bundle
from release import Releases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--wheels", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    manager = Releases(root, sys.executable)
    # 两个合成版本号用于演练，内容均为当前候选；不能当作真实发布记录。
    versions = ("1" * 40, "2" * 40)
    for version in versions:
        bundle = root / ("exercise-" + version + ".tar.gz")
        checksum = make_bundle(version, args.wheels, Path(__file__).with_name("runtime.lock"), bundle)
        assert manager.deploy(bundle, version, checksum) == "activated"
        assert manager.deploy(bundle, version, checksum) == "already_current"
        repacked = root / ("repacked-" + version + ".tar.gz")
        assert make_bundle(version, args.wheels, Path(__file__).with_name("runtime.lock"), repacked) == checksum
        assert manager.deploy(repacked, version, checksum) == "already_current"
    assert manager.pointer("previous") == versions[0]
    manager.activate(versions[0])
    assert manager.pointer("current") == versions[0]
    assert manager.pointer("previous") == versions[1]
    interrupted = "3" * 40
    bundle = root / "interruption.tar.gz"
    checksum = make_bundle(interrupted, args.wheels, Path(__file__).with_name("runtime.lock"), bundle)
    release = root / "releases" / interrupted
    # 用正常 CLI 和真实解释器开始安装，再对本次独立进程组注入 SIGKILL。
    # 这是故障注入演练，不代表在生产上复现过中断。
    with bundle.open("rb") as stream:
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name("release.py")),
                                  "--root", str(root), "--python", sys.executable,
                                  "deploy", interrupted, checksum], stdin=stream,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while not (release / "venv/pyvenv.cfg").exists():
                assert child.poll() is None, "installer_exited_before_fault_injection"
                assert time.monotonic() < deadline, "installation_boundary_not_observed"
                time.sleep(0.01)
            assert not (release / "ready.json").exists()
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
    assert release.exists() and not (release / "ready.json").exists()
    assert manager.pointer("current") == versions[0]
    with bundle.open("rb") as stream:
        retried = subprocess.run([sys.executable, str(Path(__file__).with_name("release.py")),
                                  "--root", str(root), "--python", sys.executable,
                                  "deploy", interrupted, checksum], stdin=stream,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        assert retried.returncode == 0, "real_cli_retry_failed"
    assert manager.pointer("current") == interrupted
    assert not list((root / "incoming").iterdir())
    manager.activate(versions[0])
    print("SIGKILL_INSTALL_RETRY_AND_REPACK_IDEMPOTENCY_PASSED")
    print("OFFLINE_INSTALL_ATOMIC_SWITCH_IDEMPOTENCY_ROLLBACK_PASSED")


if __name__ == "__main__":
    main()
