"""管理员显式启用的调度挂载；不创建目录、监听器、凭据或另一个发送进程。"""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

CONFIG = Path('/etc/ai-brief-scheduler.json')
IPC = Path('/run/ai-brief-scheduler')
NODE = Path('/opt/ai-brief-scheduler/node-v24.22.2/bin/node')
VERIFIERS = Path('/opt/ai-brief-scheduler/verifiers')
LABEL = 'sfcloud.ai.scheduler-binding'
FIELDS = {'activation_date', 'publisher_uid', 'socket_path', 'node_path', 'verifier_path'}
IMPORT_VERIFIER = 'const entry=process.argv[1]; process.argv.splice(1); await import(entry)'


def admin_path(path, directory=False):
    """全部祖先与内容由 root 管理，拒绝链接、硬链接及其他账号可写路径。"""
    info = path.lstat()
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if (not path.is_absolute() or path.resolve() != path or not predicate(info.st_mode)
            or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise ValueError('unsafe_scheduler_admin_path')
    for parent in path.parents:
        item = parent.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o022:
            raise ValueError('unsafe_scheduler_admin_parent')
    return info


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_scheduler_field')
        result[key] = value
    return result


class SchedulerDeployment:
    def __init__(self, uid, gid):
        admin_path(CONFIG)
        raw = CONFIG.read_bytes()
        if len(raw) > 8192:
            raise ValueError('scheduler_config_too_large')
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict) or set(value) not in (FIELDS, FIELDS | {'automatic_delivery'}):
            raise ValueError('exact_scheduler_configuration_required')
        if 'automatic_delivery' in value and value['automatic_delivery'] != 'scheduled_only':
            raise ValueError('explicit_scheduled_only_mode_required')
        if (type(uid) is not int or type(gid) is not int or min(uid, gid) <= 0
                or type(value['publisher_uid']) is not int or value['publisher_uid'] <= 0
                or value['publisher_uid'] == uid):
            raise ValueError('distinct_scheduler_identities_required')
        if (not isinstance(value['activation_date'], str)
                or dt.date.fromisoformat(value['activation_date']).isoformat() != value['activation_date']):
            raise ValueError('canonical_scheduler_activation_required')
        if value['socket_path'] != str(IPC / 'scheduled.sock') or value['node_path'] != str(NODE):
            raise ValueError('fixed_scheduler_paths_required')
        if not isinstance(value['verifier_path'], str):
            raise ValueError('pinned_scheduler_verifier_required')
        script = Path(value['verifier_path'])
        if (script.name != 'verify-scheduled-delivery.mjs' or script.parent.name != 'scripts'
                or script.parent.parent.parent != VERIFIERS
                or not re.fullmatch('[a-f0-9]{40}', script.parent.parent.name)):
            raise ValueError('pinned_scheduler_verifier_required')
        self.verifier = script.parent.parent
        admin_path(script)
        node = admin_path(NODE)
        if not node.st_mode & 0o111 or not node.st_mode & 0o004:
            raise ValueError('scheduler_node_not_executable')
        admin_path(IPC.parent, directory=True)
        ipc = IPC.lstat()
        if (IPC.resolve() != IPC or not stat.S_ISDIR(ipc.st_mode)
                or (ipc.st_uid, ipc.st_gid) != (uid, gid) or stat.S_IMODE(ipc.st_mode) != 0o711):
            raise ValueError('unsafe_scheduler_ipc_directory')
        admin_path(self.verifier, directory=True)
        files, total = {}, 0
        # 依赖也属于固定可信树；安装时须物化包内链接，不能挂载可变的 Publisher 工作目录。
        for path in self.verifier.rglob('*'):
            info = admin_path(path, directory=path.is_dir())
            if path.is_file():
                total += info.st_size
                if len(files) >= 20000 or total > 512 * 1024 * 1024:
                    raise ValueError('scheduler_verifier_tree_too_large')
                files[path.relative_to(self.verifier).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.value = value
        self.uid, self.gid = uid, gid
        self.identity = {'uid': uid, 'gid': gid,
                         'node_sha256': hashlib.sha256(NODE.read_bytes()).hexdigest(), 'files': files}
        self.binding = self.binding_for(value)

    def binding_for(self, value):
        return hashlib.sha256(json.dumps({'config': value, **self.identity},
                             sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def mounts(self, readonly_ipc=False):
        values = [(CONFIG, True), (NODE, True), (self.verifier, True), (IPC, readonly_ipc)]
        result = []
        for path, readonly in values:
            result += ['--mount', 'type=bind,src=' + str(path) + ',dst=' + str(path)
                       + (',readonly' if readonly else '')]
        return result

    def serve_args(self):
        return ['--scheduler-config', str(CONFIG)]

    def preflight(self, driver, release, require_scheduled_only=False, require_revision_notices=False):
        """无网络、无账本和凭据挂载，先验证候选及回滚版本能加载固定依赖。"""
        code = driver.code(release)
        if code == '/legacy':
            raise ValueError('scheduler_legacy_runtime_unsupported')
        name = 'ai-brief-scheduler-preflight-' + uuid.uuid4().hex
        check = (
            'import inspect,sys,subprocess; from pathlib import Path; '
            'sys.path.insert(0,sys.argv[1]); '
            'import runtime,scheduled_runtime,scheduled_delivery,scheduled_socket; '
            'assert "scheduler_config" in inspect.signature(runtime.serve).parameters; '
            'assert runtime.SCHEDULER_DRAIN_VERSION==1; '
            + ('assert getattr(runtime,"SCHEDULED_ONLY_VERSION",0)==1; ' if require_scheduled_only else '')
            + (f'assert getattr(runtime,"REVISION_NOTICES_VERSION",0)>={int(require_revision_notices)}, "revision_ledger_requires_compatible_release"; ' if require_revision_notices else '')
            +
            'assert "can_admit" in inspect.signature(scheduled_runtime.ScheduledRuntime.open).parameters; '
            'c=scheduled_runtime.load_config(Path(sys.argv[2])); '
            'assert c.get("automatic_delivery")!="scheduled_only" or getattr(runtime,"SCHEDULED_ONLY_VERSION",0)==1; '
            'assert subprocess.check_output([c["node_path"],"--version"]).strip()==b"v24.22.2"; '
            'subprocess.run([c["node_path"],"--input-type=module","-e",'
            + repr(IMPORT_VERIFIER) + ',c["verifier_path"]],check=True,timeout=30)'
        )
        args = ['run', '--rm', '--pull', 'never', '--name', name, '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--user', str(driver.config['uid']) + ':' + str(driver.config['gid']),
                '--cpus', '1', '--memory', '768m', '--pids-limit', '64', '--ipc', 'private',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
                '--mount', 'type=bind,src=' + driver.config['root'] + '/releases/' + release
                + ',dst=/prototype/releases/' + release + ',readonly']
        args += self.mounts(readonly_ipc=True)
        args += [driver.config['image'], 'python', '-I', '-B', '-c', check, code, str(CONFIG)]
        try:
            driver.command(args, timeout=60)
        finally:
            driver.command(['rm', '-f', name], optional=True, timeout=15)

    def assert_container(self, item):
        """同一配置绑定必须跨重启和版本切换保持；绝不把缺失挂载当作健康。"""
        config, host = item['Config'], item['HostConfig']
        expected = {str(CONFIG): (str(CONFIG), False), str(NODE): (str(NODE), False),
                    str(self.verifier): (str(self.verifier), False), str(IPC): (str(IPC), True)}
        mounts = {m['Destination']: (m['Source'], m['RW']) for m in item['Mounts']}
        cmd = config.get('Cmd') or []
        if (config.get('Labels', {}).get(LABEL) != self.binding
                or config.get('User') != str(self.uid) + ':' + str(self.gid)
                or cmd[-2:] != self.serve_args()
                or any(mounts.get(k) != v for k, v in expected.items())
                or {k for k, v in mounts.items() if v[1]} != {'/relay/state', '/relay/run', str(IPC)}
                or not host.get('ReadonlyRootfs') or host.get('Privileged')
                or host.get('CapDrop') != ['ALL'] or host.get('GroupAdd')
                or host.get('SecurityOpt') != ['no-new-privileges']
                or host.get('UsernsMode') != '' or host.get('PortBindings')):
            raise ValueError('scheduler_container_binding_changed')
