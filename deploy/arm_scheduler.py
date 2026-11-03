#!/usr/bin/python3
"""管理员为下一次受控发布准备调度开关；不重启、不准入、不发送。"""

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

if __package__:
    from .policy import Policy
    from .scheduler_service import unique, admin_path
    from .stage_helper import CONFIG, CONTROL_ROOT, root_owned_file, docker_command
    from .supervisor import DockerDriver
else:
    from policy import Policy
    from scheduler_service import unique, admin_path
    from stage_helper import CONFIG, CONTROL_ROOT, root_owned_file, docker_command
    from supervisor import DockerDriver


def replace_config(path, value, mode):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write((json.dumps(value, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def arm(release, binding, config_path=CONFIG, control_root=CONTROL_ROOT, factory=DockerDriver):
    if not re.fullmatch('[a-f0-9]{40}', release) or not re.fullmatch('[a-f0-9]{64}', binding):
        raise ValueError('exact_scheduler_release_and_binding_required')
    root_owned_file(config_path)
    root_owned_file(control_root / 'policy.json')
    before = config_path.read_bytes()
    if len(before) > 16384:
        raise ValueError('stage_config_too_large')
    config = json.loads(before, object_pairs_hook=unique)
    docker_command(config, release, '0' * 64)
    if config.get('live_enabled') is not True:
        raise ValueError('live_controller_required')
    for name in ('switch-pending.json', 'control-pending.json'):
        if (control_root / name).exists():
            raise ValueError('pending_operation_requires_reconciliation')
    if Policy(control_root).value['active'] != release:
        raise ValueError('active_release_changed')
    config['live']['scheduler'] = True
    driver = factory(config, control_root)
    if driver.scheduler_binding() != binding:
        raise ValueError('scheduler_binding_changed')
    # 当前应用将成为下一版的回滚目标，必须先通过完整兼容性预检。
    driver.preflight(release)
    driver.preflight_rollback(release)
    status = driver.status(release)
    if (driver.current_sender() != release or status.get('release') != release
            or status.get('phase') != 'active' or status.get('in_flight') is not False
            or status.get('reconciled') is not True or status.get('delivery_ready') is not True):
        raise ValueError('active_release_not_drained_and_healthy')
    if (config_path.read_bytes() != before or Policy(control_root).value['active'] != release
            or any((control_root / n).exists() for n in ('switch-pending.json', 'control-pending.json'))):
        raise ValueError('preparation_inputs_changed')
    root_owned_file(config_path)
    replace_config(config_path, config, stat.S_IMODE(config_path.stat().st_mode))
    return {'status': 'prepared_for_next_release', 'active': release, 'scheduler_binding': binding,
            'previous_config_sha256': hashlib.sha256(before).hexdigest(), 'activation_performed': False}


@contextmanager
def administrative_locks(root=CONTROL_ROOT):
    import fcntl
    admin_path(root, directory=True)
    descriptors = []
    try:
        # 与版本化入口采用同一顺序；先阻止创建控制切换意图，再锁应用部署。
        for name in ('control-entry.lock', 'deployment.lock'):
            fd = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('unsafe_administrator_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def main():
    if os.getuid() != 0 or len(sys.argv) != 4 or sys.argv[1] != '--arm':
        raise ValueError('explicit_administrator_arm_required')
    with administrative_locks():
        print(json.dumps(arm(sys.argv[2], sys.argv[3])))


if __name__ == '__main__':
    main()
