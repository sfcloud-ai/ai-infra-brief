"""在隔离目录实际安装 wheel，验证发布、幂等和回滚；不接触 relay。"""

import argparse
from pathlib import Path
import sys

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
    assert manager.pointer("previous") == versions[0]
    manager.activate(versions[0])
    assert manager.pointer("current") == versions[0]
    assert manager.pointer("previous") == versions[1]
    print("OFFLINE_INSTALL_ATOMIC_SWITCH_IDEMPOTENCY_ROLLBACK_PASSED")


if __name__ == "__main__":
    main()
