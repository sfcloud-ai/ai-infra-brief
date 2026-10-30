"""固定配置的单实例交接器；无真实配置时不可启动，状态绝不随代码回滚。"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

if __package__:
    from .release import write_json
else:
    from release import write_json


class ServiceError(Exception):
    pass


class Supervisor:
    def __init__(self, root, driver, policy):
        self.root, self.driver, self.policy = Path(root), driver, policy
        self.pending = self.root / "switch-pending.json"

    def rollback_interrupted(self):
        if not self.pending.exists():
            return
        pending = json.loads(self.pending.read_text())
        before, target = pending["before"], pending["target"]
        # 只有持久记录证明从未发起激活时，才允许不经控制 socket 终止候选。
        if pending.get("activation_attempted", True) is False:
            self.driver.abort_start(target, before)
        running = self.driver.current_sender()
        if running:
            if running not in {before, target}:
                raise ServiceError("unexpected_running_release")
            self.driver.quiesce(running)
        self.driver.handoff(before)
        self.driver.start_paused(before)
        self.driver.wait_ready(before)
        self.driver.activate(before)
        self.driver.wait_healthy(before)
        self.policy.completed(before)
        self.pending.unlink()

    def switch(self, target):
        if self.pending.exists():
            self.rollback_interrupted()
            raise ServiceError("interrupted_switch_recovered_retry_explicitly")
        # 删除过的历史版本或损坏的代码必须在停止现有发送者前被拒绝。
        self.driver.preflight(target)
        before = self.policy.value["active"]
        if before is None:
            before = self.driver.capture_legacy()
            self.policy.completed(before)
        if target == before:
            self.driver.wait_healthy(target)
            return
        pending = {"before": before, "target": target, "activation_attempted": False}
        write_json(self.pending, pending)
        stopped = False
        try:
            self.driver.quiesce(before)
            stopped = True
            self.driver.handoff(target)
            self.driver.start_paused(target)
            self.driver.wait_ready(target)
            # 先落盘再发激活；中断或应答丢失后都按可能已发送处理。
            pending["activation_attempted"] = True
            write_json(self.pending, pending)
            self.driver.activate(target)
            self.driver.wait_healthy(target)
            self.policy.completed(target)
            self.pending.unlink()
        except Exception:
            if stopped or self.driver.current_sender() is None:
                # 若新实例无法确认已排空，就保持 pending，不冒险启动第二个发送者。
                self.rollback_interrupted()
            # 未确认停止旧实例时保留 pending，恢复流程必须再次核实现场。
            raise


class DockerDriver:
    def __init__(self, config, control_root):
        self.config, self.control = config, Path(control_root)
        required = {"container", "legacy_container", "relay_root", "control_code", "legacy_relay", "legacy_images"}
        live = config["live"]
        if set(live) != required:
            raise ServiceError("invalid_live_configuration")
        for name in ("container", "legacy_container"):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}", live[name]):
                raise ServiceError("invalid_container_name")
        if live["container"] == live["legacy_container"]:
            raise ServiceError("legacy_and_managed_names_must_differ")
        for name in ("relay_root", "control_code"):
            path = Path(live[name])
            if not path.is_absolute() or path.resolve() != path or not path.is_dir() or "," in str(path):
                raise ServiceError("unsafe_live_path")
        for field, basename in (("legacy_relay", "relay.py"), ("legacy_images", "images.py")):
            path = Path(live[field])
            if not path.is_absolute() or path.name != basename or ".." in path.parts:
                raise ServiceError("invalid_legacy_code_path")
        self.live = live
        self.legacy_path = self.control / "legacy.json"
        self.activation = self.control / "activation"
        self.activation.mkdir(mode=0o755, exist_ok=True)

    def command(self, arguments, timeout=40, optional=False):
        result = subprocess.run(["/usr/bin/docker"] + arguments, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout,
                                env={"PATH": "/usr/bin:/bin", "HOME": "/root"})
        if result.returncode and not optional:
            raise ServiceError("docker_operation_failed")
        return result

    def inspect(self, name):
        result = self.command(["inspect", name], optional=True)
        return json.loads(result.stdout)[0] if result.returncode == 0 else None

    def legacy(self):
        return json.loads(self.legacy_path.read_text()) if self.legacy_path.exists() else None

    def capture_legacy(self):
        if not self.config.get("bootstrap_approved"):
            raise ServiceError("first_cutover_not_approved")
        existing = self.legacy()
        if existing:
            return existing["release"]
        old = self.inspect(self.live["legacy_container"])
        if not old or not old["State"]["Running"] or old["Image"] != self.config["image"]:
            raise ServiceError("legacy_identity_or_image_mismatch")
        if (old["Config"].get("User") != str(self.config["uid"]) + ":" + str(self.config["gid"])
                or not any(mount["Destination"] == "/relay" and mount["Source"] == self.live["relay_root"]
                           for mount in old["Mounts"])):
            raise ServiceError("legacy_user_or_state_root_mismatch")
        directory = self.control / "legacy-code"
        content = []
        with tempfile.TemporaryDirectory(prefix="legacy-copy-", dir=self.control) as temporary:
            staging = Path(temporary) / "code"
            staging.mkdir(mode=0o755)
            for name, field in (("relay.py", "legacy_relay"), ("images.py", "legacy_images")):
                path = staging / name
                self.command(["cp", self.live["legacy_container"] + ":" + self.live[field], str(path)])
                if path.is_symlink() or not path.is_file():
                    raise ServiceError("unsafe_legacy_code")
                content.append(path.read_bytes())
                os.chmod(path, 0o444)
            if directory.exists():
                if directory.is_symlink() or any((directory / name).read_bytes() != (staging / name).read_bytes()
                                                for name in ("relay.py", "images.py")):
                    raise ServiceError("legacy_snapshot_conflict")
            else:
                os.replace(staging, directory)
        release = hashlib.sha256(b"\0".join(content)).hexdigest()[:40]
        write_json(self.legacy_path, {"release": release, "restart": old["HostConfig"]["RestartPolicy"]["Name"]})
        return release

    def code(self, release):
        if not re.fullmatch(r"[0-9a-f]{40}", release):
            raise ServiceError("invalid_release")
        legacy = self.legacy()
        if legacy and legacy["release"] == release:
            return "/legacy"
        return "/prototype/releases/" + release + "/venv/lib/python3.12/site-packages/ai_brief_relay"

    def one_shot(self, arguments, readonly_state=False):
        name = "ai-brief-check-" + uuid.uuid4().hex
        try:
            return self.command(["run", "--rm", "--pull", "never", "--name", name, "--network", "none"]
                                + self.common(readonly_state) + [self.config["image"], "python",
                                "/control/runtime.py"] + arguments, timeout=90)
        finally:
            self.command(["rm", "-f", name], optional=True, timeout=15)

    def preflight(self, release):
        code = self.code(release)
        if code != "/legacy":
            ready = Path(self.config["root"]) / "releases" / release / "ready.json"
            if (not ready.is_file() or ready.is_symlink()
                    or json.loads(ready.read_text()).get("commit") != release):
                raise ServiceError("release_not_retained_or_ready")
        result = self.one_shot(["capabilities", "--code", code], readonly_state=True)
        if json.loads(result.stdout) != {"relay": True, "resume": True, "pause_activate": True, "messages_sent": 0}:
            raise ServiceError("release_capabilities_failed")

    def common(self, readonly_state=False):
        uid = str(self.config["uid"])
        gid = str(self.config["gid"])
        root = self.live["relay_root"]
        flags = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--user", uid + ":" + gid, "--cpus", "1", "--memory", "768m", "--pids-limit", "128",
                 "--ipc", "private", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,uid=" + uid + ",gid=" + gid,
                 "--mount", "type=bind,src=" + self.config["root"] + ",dst=/prototype,readonly",
                 "--mount", "type=bind,src=" + self.live["control_code"] + ",dst=/control,readonly",
                 "--mount", "type=bind,src=" + str(self.activation) + ",dst=/activation,readonly",
                 "--mount", "type=bind,src=" + root + ",dst=/relay,readonly",
                 "--mount", "type=bind,src=" + root + "/state,dst=/relay/state" + (",readonly" if readonly_state else ""),
                 "--mount", "type=bind,src=" + root + "/run,dst=/relay/run" + (",readonly" if readonly_state else ""),
                 "-e", "PYTHONDONTWRITEBYTECODE=1"]
        if self.legacy():
            flags += ["--mount", "type=bind,src=" + str(self.control / "legacy-code") + ",dst=/legacy,readonly"]
        return flags

    def running_release(self):
        current = self.inspect(self.live["container"])
        if not current:
            return None
        labels = current["Config"].get("Labels") or {}
        if labels.get("sfcloud.ai.service") != "ai-infra-brief":
            raise ServiceError("managed_container_name_collision")
        return labels.get("sfcloud.ai.release") if current["State"]["Running"] else None

    def current_sender(self):
        managed = self.running_release()
        old = self.inspect(self.live["legacy_container"])
        legacy_running = bool(old and old["State"]["Running"])
        if managed and legacy_running:
            raise ServiceError("multiple_senders_detected")
        if managed:
            return managed
        if legacy_running:
            legacy = self.legacy()
            if not legacy:
                raise ServiceError("legacy_not_captured")
            return legacy["release"]
        return None

    def status(self, release, operation="status", deadline=None, nonce=None):
        # 宿主先定截止时间，容器内残留的 exec 不能因启动较晚而延长授权。
        if deadline is None:
            deadline = time.monotonic() + (180 if operation == "activate" else 1800)
        if self.running_release() != release:
            raise ServiceError("running_release_mismatch")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("control_deadline")
        arguments = ["exec", self.live["container"], "python", "/control/runtime.py", "control",
                     "--root", "/relay", "--code", self.code(release), "--release", release,
                     "--operation", operation, "--deadline", repr(deadline)]
        if nonce is not None:
            arguments += ["--activation-nonce", nonce]
        result = self.command(arguments, timeout=remaining + 15)
        return json.loads(result.stdout)

    def wait(self, release, active=False):
        deadline = time.monotonic() + (1800 if active else 180)
        while time.monotonic() < deadline:
            try:
                status = self.status(release, deadline=deadline)
                if (status.get("phase") == ("active" if active else "paused")
                        and status.get("reconciled") is True and status.get("in_flight") is False
                        and (not active or status.get("successful_polls", 0) >= 1)):
                    return
            except (ServiceError, ValueError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.25)
        raise ServiceError("relay_health_deadline")

    def wait_ready(self, release):
        self.wait(release)

    def wait_healthy(self, release):
        self.wait(release, active=True)
        write_json(self.activation / "active.json", {"release": release})
        os.chmod(self.activation / "active.json", 0o444)
        self.revoke_permit()

    def revoke_permit(self):
        write_json(self.activation / "permit.json", {"release": None, "nonce": None})
        os.chmod(self.activation / "permit.json", 0o444)

    def activate(self, release):
        nonce = uuid.uuid4().hex
        write_json(self.activation / "permit.json", {"release": release, "nonce": nonce})
        os.chmod(self.activation / "permit.json", 0o444)
        result = self.status(release, "activate", nonce=nonce)
        if result.get("phase") != "active" or result.get("reconciled") is not True:
            raise ServiceError("relay_activation_failed")

    def handoff(self, release):
        self.one_shot(["handoff", "--root", "/relay", "--code", self.code(release)])

    def abort_start(self, release, before):
        # 成功查询完整容器清单后才能认定不存在；daemon 或 inspect 失败不能当作已停止。
        result = self.command(["container", "ls", "--all", "--no-trunc", "--filter",
                               "name=^/" + re.escape(self.live["container"]) + "$", "--format", "{{.ID}}"])
        identities = result.stdout.decode().split()
        if not identities:
            return
        if len(identities) != 1 or not re.fullmatch(r"[0-9a-f]{64}", identities[0]):
            raise ServiceError("unexpected_managed_container_inventory")
        identity = identities[0]
        current = json.loads(self.command(["inspect", identity]).stdout)[0]
        labels = current["Config"].get("Labels") or {}
        if labels.get("sfcloud.ai.service") != "ai-infra-brief":
            raise ServiceError("managed_container_name_collision")
        if labels.get("sfcloud.ai.release") == before:
            return
        if labels.get("sfcloud.ai.release") != release:
            raise ServiceError("unexpected_active_instance")
        for name in ("active.json", "permit.json"):
            path = self.activation / name
            if path.exists() and json.loads(path.read_text()).get("release") == release:
                raise ServiceError("candidate_activation_not_excluded")
        self.revoke_permit()
        # 未激活候选先停止重启循环，避免 update 与容器退出时的 runtime 竞态。
        self.command(["stop", "--time", "30", identity], timeout=40)
        self.command(["update", "--restart=no", identity])
        current = json.loads(self.command(["inspect", identity]).stdout)[0]
        if any(current["State"].get(name) is not False for name in ("Running", "Restarting", "Paused")):
            raise ServiceError("candidate_stop_not_confirmed")
        # 随后的 handoff 使用上一版代码，在独占锁下离线核对账本后再恢复发送。

    def quiesce(self, release):
        managed = self.running_release()
        if managed:
            if managed != release:
                raise ServiceError("unexpected_active_instance")
            # 同时撤销迟到请求和自动重启的授权，然后才请求排空。
            self.revoke_permit()
            write_json(self.activation / "active.json", {"release": None})
            os.chmod(self.activation / "active.json", 0o444)
            result = self.status(release, "pause")
            if (result.get("phase") != "paused" or result.get("reconciled") is not True
                    or result.get("in_flight") is not False):
                raise ServiceError("unreconciled_delivery_blocks_stop")
            self.command(["update", "--restart=no", self.live["container"]])
            self.command(["stop", "--time", "30", self.live["container"]], timeout=40)
            if self.running_release():
                raise ServiceError("old_instance_still_running")
            return
        legacy = self.legacy()
        old = self.inspect(self.live["legacy_container"])
        if not legacy or release != legacy["release"] or not old or not old["State"]["Running"]:
            raise ServiceError("expected_old_instance_not_running")
        # 旧入口没有 drain。先冻结，再只读确认没有 sending/unknown，最后终止冻结实例。
        if not old["State"].get("Paused"):
            self.command(["pause", self.live["legacy_container"]])
        try:
            self.one_shot(["check-state", "--root", "/relay", "--code", "/legacy"], readonly_state=True)
        except Exception:
            self.command(["unpause", self.live["legacy_container"]])
            raise
        self.command(["update", "--restart=no", self.live["legacy_container"]])
        self.command(["kill", "--signal", "KILL", self.live["legacy_container"]])
        old = self.inspect(self.live["legacy_container"])
        if old and old["State"]["Running"]:
            raise ServiceError("legacy_stop_not_confirmed")

    def start_paused(self, release):
        if self.running_release():
            raise ServiceError("refuse_parallel_sender")
        old = self.inspect(self.live["legacy_container"])
        if old and old["State"]["Running"]:
            raise ServiceError("legacy_sender_still_running")
        if self.inspect(self.live["container"]):
            self.command(["rm", self.live["container"]])
        (self.activation / "active.json").unlink(missing_ok=True)
        self.revoke_permit()
        self.command(["run", "-d", "--pull", "never", "--name", self.live["container"],
                      "--label", "sfcloud.ai.service=ai-infra-brief", "--label", "sfcloud.ai.release=" + release,
                      "--restart", "unless-stopped", "--network", "bridge"] + self.common()
                     + [self.config["image"], "python", "/control/runtime.py", "serve", "--root", "/relay",
                        "--code", self.code(release), "--release", release,
                        "--activation-file", "/activation/active.json"])
