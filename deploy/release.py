"""原型离线包接收器：只更新独立目录，不操作系统服务或真实投递状态。"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

MAX_BUNDLE = 64 * 1024 * 1024
MAX_CONTENT = 128 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")


class ReleaseError(Exception):
    pass


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unpack_bundle(archive_path, destination, commit, checksum):
    if not SHA.fullmatch(commit) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ReleaseError("invalid_identity")
    raw = archive_path.read_bytes()
    if len(raw) > MAX_BUNDLE or hashlib.sha256(raw).hexdigest() != checksum:
        raise ReleaseError("bundle_hash_or_size")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = []
        total = 0
        for item in archive:
            members.append(item)
            total += item.size
            if item.size < 0 or len(members) > 32 or total > MAX_CONTENT:
                raise ReleaseError("archive_limits_exceeded")
        names = [item.name for item in members]
        if len(names) > 32 or len(set(names)) != len(names) or "manifest.json" not in names:
            raise ReleaseError("invalid_members")
        if sum(item.size for item in members) > MAX_CONTENT:
            raise ReleaseError("bundle_expands_too_large")
        content = {}
        for item in members:
            if not item.isfile() or not (item.name == "manifest.json" or
                    re.fullmatch(r"wheels/[A-Za-z0-9_.-]+\.whl", item.name)):
                raise ReleaseError("unsafe_archive_member")
            with archive.extractfile(item) as stream:
                content[item.name] = stream.read(item.size + 1)
        if len(content["manifest.json"]) > 65536:
            raise ReleaseError("manifest_too_large")
        manifest = json.loads(content.pop("manifest.json"))
        if (set(manifest) != {"version", "commit", "python", "platform", "files"}
                or manifest["version"] != 1 or manifest["commit"] != commit
                or manifest["python"] != "3.12" or manifest["platform"] != "linux-x86_64"
                or manifest["files"] != {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
                or not content):
            raise ReleaseError("manifest_mismatch")
        destination.mkdir(mode=0o700)
        (destination / "wheels").mkdir(mode=0o700)
        for name, data in content.items():
            (destination / name).write_bytes(data)
        write_json(destination / "manifest.json", manifest)
    return manifest


class Releases:
    def __init__(self, root, runtime):
        self.root, self.runtime = Path(root).absolute(), str(Path(runtime).absolute())
        info = self.root.lstat()
        if (self.root.resolve() != self.root or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022):
            raise ReleaseError("unsafe_root")
        for name in ("releases", "checks", "incoming"):
            path = self.root / name
            path.mkdir(mode=0o700, exist_ok=True)
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
                raise ReleaseError("unsafe_subdirectory")

    def pointer(self, name):
        path = self.root / name
        if not path.is_symlink():
            if path.exists():
                raise ReleaseError("unexpected_pointer_file")
            return None
        target = os.readlink(path)
        if not re.fullmatch(r"releases/[0-9a-f]{40}", target):
            raise ReleaseError("unsafe_pointer")
        release = self.root / target
        if release.is_symlink() or not release.is_dir():
            raise ReleaseError("missing_pointer_target")
        return target.split("/")[1]

    def point(self, name, commit):
        path = self.root / name
        self.pointer(name)
        if commit is None:
            path.unlink(missing_ok=True)
            return
        if not SHA.fullmatch(commit):
            raise ReleaseError("invalid_commit")
        temporary = self.root / (name + ".next")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to("releases/" + commit)
        os.replace(temporary, path)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def run(self, command, timeout=120):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                     "PYTHONNOUSERSITE": "1", "PIP_CONFIG_FILE": os.devnull,
                                     "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
        if result.returncode:
            raise ReleaseError("isolated_command_failed")
        return result.stdout

    def install(self, release):
        version = self.run([self.runtime, "-I", "-c", "import sys;print('.'.join(map(str,sys.version_info[:2])))"])
        if version.strip() != b"3.12":
            raise ReleaseError("python312_required")
        # 直接在最终目录创建 venv；移动已创建的 venv 会破坏入口脚本的绝对路径。
        self.run([self.runtime, "-I", "-m", "venv", str(release / "venv")])
        python = str(release / "venv/bin/python")
        self.run([python, "-I", "-m", "pip", "install", "--no-index", "--no-deps", "--only-binary=:all:"]
                 + [str(path) for path in sorted((release / "wheels").glob("*.whl"))])
        self.run([python, "-I", "-m", "pip", "check"])

    def health(self, commit):
        release = self.root / "releases" / commit
        if release.is_symlink() or not release.is_dir():
            raise ReleaseError("unsafe_release")
        with tempfile.TemporaryDirectory(prefix="health-", dir=self.root / "checks") as directory:
            result = json.loads(self.run([str(release / "venv/bin/python"), "-I", "-m", "ai_brief",
                                         "--state-dir", directory + "/mock-state", "run"]))
            if result["manifest"]["delivery"]["real_status"] != "not_sent":
                raise ReleaseError("unexpected_delivery")

    def recover(self):
        path = self.root / "pending.json"
        if path.exists():
            pending = json.loads(path.read_text())
            self.point("current", pending["before"])
            self.point("previous", pending["previous"])
            path.unlink()

    def activate(self, commit):
        if not SHA.fullmatch(commit):
            raise ReleaseError("invalid_commit")
        release = self.root / "releases" / commit
        if not (release / "ready.json").is_file():
            raise ReleaseError("release_not_ready")
        self.health(commit)
        before, previous = self.pointer("current"), self.pointer("previous")
        if before == commit:
            return "already_current"
        pending = self.root / "pending.json"
        write_json(pending, {"before": before, "previous": previous, "target": commit})
        try:
            self.point("current", commit)
            self.health(commit)
            self.point("previous", before)
        except BaseException:
            self.point("current", before)
            self.point("previous", previous)
            pending.unlink()
            raise
        pending.unlink()
        return "activated"

    def deploy(self, archive, commit, checksum):
        release = self.root / "releases" / commit
        if not SHA.fullmatch(commit):
            raise ReleaseError("invalid_commit")
        if release.exists() or release.is_symlink():
            if release.is_symlink() or not release.is_dir():
                raise ReleaseError("unsafe_release")
            ready = release / "ready.json"
            if not ready.exists() or json.loads(ready.read_text())["bundle_sha256"] != checksum:
                raise ReleaseError("existing_release_conflict")
        else:
            try:
                unpack_bundle(archive, release, commit, checksum)
                self.install(release)
                self.health(commit)
                write_json(release / "ready.json", {"commit": commit, "bundle_sha256": checksum})
            except BaseException:
                if release.is_dir() and not release.is_symlink():
                    shutil.rmtree(release)
                raise
        return self.activate(commit)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("operation", choices=("deploy", "rollback"))
    parser.add_argument("commit")
    parser.add_argument("checksum", nargs="?")
    args = parser.parse_args()
    manager = Releases(args.root, args.python)
    fd = os.open(manager.root / "deployment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manager.recover()
        if args.operation == "rollback":
            result = manager.activate(args.commit)
        else:
            with tempfile.TemporaryDirectory(prefix="upload-", dir=manager.root / "incoming") as directory:
                path = Path(directory) / "bundle.tar.gz"
                count = 0
                with path.open("wb") as target:
                    while True:
                        data = sys.stdin.buffer.read(65536)
                        if not data:
                            break
                        count += len(data)
                        if count > MAX_BUNDLE:
                            raise ReleaseError("bundle_too_large")
                        target.write(data)
                result = manager.deploy(path, args.commit, args.checksum or "")
        print(json.dumps({"prototype_release": result, "commit": args.commit, "services_changed": False}))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"prototype_release_failed"}', file=sys.stderr)
        raise SystemExit(1)
