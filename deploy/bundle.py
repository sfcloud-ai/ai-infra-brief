"""只打包已构建的应用和校验过哈希的离线 wheel，不包含源码或配置。"""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def make_bundle(commit, wheel_dir, lock_path, output):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid_commit")
    expected = {}
    for line in Path(lock_path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"([\w-]+)==([\w.]+) --hash=sha256:([0-9a-f]{64})", line)
        if not match:
            raise ValueError("invalid_dependency_lock")
        name, version, sha = match.groups()
        expected[(name.replace("-", "_"), version)] = sha
    files = {}
    seen = set()
    apps = []
    for path in sorted(Path(wheel_dir).iterdir()):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", path.name):
            raise ValueError("unexpected_wheel_file")
        pieces = path.name.split("-")
        identity = tuple(pieces[:2])
        data = path.read_bytes()
        if identity[0] == "sfcloud_ai":
            apps.append(path.name)
            # 包内路径也必须是普通文件；wheel 的入口和资源由打包回归验证。
            with zipfile.ZipFile(path) as archive:
                for item in archive.infolist():
                    if item.filename.startswith("/") or ".." in item.filename.split("/"):
                        raise ValueError("invalid_wheel_path")
        elif identity not in expected or digest(data) != expected[identity] or identity in seen:
            raise ValueError("dependency_hash_or_identity_mismatch")
        else:
            seen.add(identity)
        files["wheels/" + path.name] = data
    if len(apps) != 1 or seen != set(expected):
        raise ValueError("incomplete_wheel_set")
    manifest = {"version": 1, "commit": commit, "python": "3.12", "platform": "linux-x86_64",
                "files": {name: digest(data) for name, data in files.items()}}
    files["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    # gzip 默认会记录当前时间和输出文件名，两者都不属于发布内容。
    with Path(output).open("wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(data), 0o600, 0
                    archive.addfile(info, io.BytesIO(data))
    return digest(Path(output).read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--wheels", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(make_bundle(args.commit, args.wheels, Path(__file__).with_name("runtime.lock"), args.output))
