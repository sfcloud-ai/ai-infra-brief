#!/usr/bin/python3
"""稳定的受限信任入口；仓库控制代码按通过 CI 的 main 提交自动版本化。"""

import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path('/var/lib/ai-brief-control')
REPOSITORY = 'sfcloud-ai/ai-infra-brief'
FILES = ('stage_helper.py', 'policy.py', 'release.py', 'supervisor.py', 'notice_service.py', 'ssh_entry.py')
SHA = re.compile(r'[0-9a-f]{40}')
HASH = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*')


class CandidatePending(Exception):
    """主线 CI 尚未结束，可以在产生任何切换意图前短暂等待。"""


def deployment_input(name):
    return (name.endswith('.py') or name.startswith('deploy/')
            or name in ('pyproject.toml', 'LICENSE', '.github/workflows/cd.yml', '.github/workflows/rollback.yml')
            or ('/' not in name and fnmatch.fnmatchcase(name, 'requirements*.lock')))


def admin(path, directory=False):
    """所有载入的文件及父目录必须由 root 持有且不可被服务账号改写。"""
    info = path.lstat()
    predicate = stat.S_ISDIR if directory else stat.S_ISREG
    if (path.resolve() != path or not predicate(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or (not directory and info.st_nlink != 1)):
        raise ValueError('unsafe_control_code')
    for parent in path.parents:
        value = parent.lstat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise ValueError('unsafe_control_parent')


def atomic(path, data, mode=0o600):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    atomic(path, (json.dumps(value, sort_keys=True) + '\n').encode())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('control_source_redirect')


def fetch(url, limit):
    if not (url.startswith('https://api.github.com/repos/' + REPOSITORY + '/')
            or url.startswith('https://raw.githubusercontent.com/' + REPOSITORY + '/')):
        raise ValueError('unexpected_control_source')
    request = urllib.request.Request(url, headers={'User-Agent': 'ai-brief-control-cd', 'Accept': 'application/vnd.github+json'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('control_source_limit')
    return data


def api(path):
    return json.loads(fetch('https://api.github.com/repos/' + REPOSITORY + path, 2 * 1024 * 1024))


def trusted_candidate(commit, previous, read=api):
    """独立验证 main、前进关系及该提交最新的主线 CI，不信任上传者声明。"""
    if not SHA.fullmatch(commit) or not SHA.fullmatch(previous):
        raise ValueError('invalid_control_commit')
    ref = read('/git/ref/heads/main')['object']
    if ref.get('type') != 'commit' or not SHA.fullmatch(ref.get('sha', '')):
        raise ValueError('control_target_not_current_main')
    if ref['sha'] != commit:
        latest = read('/compare/' + commit + '...' + ref['sha'] + '?per_page=1')
        files = latest.get('files')
        if (latest.get('status') != 'ahead' or latest.get('base_commit', {}).get('sha') != commit
                or latest.get('merge_base_commit', {}).get('sha') != commit or not isinstance(files, list)
                or len(files) >= 300 or any(deployment_input(f['filename']) or deployment_input(f.get('previous_filename', '')) for f in files)):
            raise ValueError('control_target_superseded')
    if commit != previous:
        comparison = read('/compare/' + previous + '...' + commit + '?per_page=1')
        if (comparison.get('status') != 'ahead' or comparison.get('base_commit', {}).get('sha') != previous
                or comparison.get('merge_base_commit', {}).get('sha') != previous):
            raise ValueError('control_target_not_forward')
    runs = read('/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=' + commit + '&per_page=20').get('workflow_runs', [])
    matching = [r for r in runs if r.get('head_sha') == commit and r.get('head_branch') == 'main'
                and r.get('event') == 'push' and r.get('path') == '.github/workflows/ci.yml'
                and r.get('repository', {}).get('full_name') == REPOSITORY]
    if not matching:
        raise CandidatePending('control_ci_missing')
    latest = max(matching, key=lambda r: (r['id'], r.get('run_attempt', 1)))
    if latest.get('status') != 'completed':
        raise CandidatePending('control_ci_pending')
    if latest.get('conclusion') != 'success':
        raise ValueError('control_ci_not_successful')
    return {'commit': commit, 'main': ref['sha'], 'run_id': latest['id'], 'run_attempt': latest.get('run_attempt', 1)}


def verify_record(record, root=ROOT):
    if (set(record) != {'commit', 'files'} or not SHA.fullmatch(record['commit'])
            or not isinstance(record['files'], dict) or not set(FILES) <= set(record['files'])
            or len(record['files']) > 128 or any(not NAME.fullmatch(name) for name in record['files'])
            or any(not HASH.fullmatch(value) for value in record['files'].values())):
        raise ValueError('invalid_control_record')
    directory = root / 'code' / record['commit']
    admin(directory, directory=True)
    paths = list(directory.rglob('*'))
    for path in paths:
        admin(path, directory=path.is_dir())
    if {p.relative_to(directory).as_posix() for p in paths if p.is_file()} != set(record['files']):
        raise ValueError('unexpected_control_file')
    for name, expected in record['files'].items():
        path = directory / name
        admin(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('control_file_changed')
    return directory


def current_record(root=ROOT):
    path = root / 'control-release.json'
    admin(path)
    record = json.loads(path.read_text())
    return record, verify_record(record, root)


def source_tree(commit, read):
    """包含部署目录的新模块及数据文件，无须为每个新增模块改稳定入口。"""
    tree_sha = read('/git/commits/' + commit)['tree']['sha']
    if not SHA.fullmatch(tree_sha):
        raise ValueError('invalid_control_tree')
    tree = read('/git/trees/' + tree_sha + '?recursive=1')
    if tree.get('truncated') is not False:
        raise ValueError('incomplete_control_tree')
    result = {}
    for item in tree['tree']:
        path = item['path']
        if not path.startswith('deploy/') or item['type'] == 'tree':
            continue
        name = path[len('deploy/'):]
        if (not NAME.fullmatch(name) or item['type'] != 'blob' or item['mode'] not in ('100644', '100755')
                or not SHA.fullmatch(item['sha']) or name in result):
            raise ValueError('invalid_control_source_file')
        result[name] = item['sha']
    if not set(FILES) <= set(result) or len(result) > 128:
        raise ValueError('control_source_file_set')
    return result


def stage(commit, previous, root=ROOT, read=api, download=fetch):
    deadline = time.monotonic() + 180
    while True:
        try:
            proof = trusted_candidate(commit, previous, read)
            break
        except CandidatePending:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)
    content = {}
    for name, blob in source_tree(commit, read).items():
        data = download('https://raw.githubusercontent.com/' + REPOSITORY + '/' + commit + '/deploy/' + name, 512 * 1024)
        if hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != blob:
            raise ValueError('control_source_blob_changed')
        # 只解析语法，尚不执行候选；主机的 Python 版本也必须能载入。
        if name.endswith('.py'):
            compile(data, name, 'exec')
        content[name] = data
        if sum(len(value) for value in content.values()) > 4 * 1024 * 1024:
            raise ValueError('control_source_size')
    if trusted_candidate(commit, previous, read) != proof:
        raise ValueError('control_inputs_changed')
    record = {'commit': commit, 'files': {n: hashlib.sha256(b).hexdigest() for n, b in content.items()}}
    parent = root / 'code'
    admin(parent, directory=True)
    target = parent / commit
    if target.exists():
        verify_record(record, root)
        return record
    temporary = parent / (commit + '.' + uuid.uuid4().hex + '.tmp')
    temporary.mkdir(mode=0o700)
    for name, data in content.items():
        (temporary / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic(temporary / name, data, 0o444)
    for directory in sorted((p for p in temporary.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)
    sync_directory(temporary)
    os.rename(temporary, target)
    sync_directory(parent)
    verify_record(record, root)
    return record


def parse(arguments):
    if arguments == ['status']:
        return tuple(arguments)
    if (len(arguments) != 4 or arguments[0] not in ('deploy', 'rollback')
            or not re.fullmatch(r'[1-9][0-9]{0,17}', arguments[1]) or not SHA.fullmatch(arguments[2])
            or not (HASH if arguments[0] == 'deploy' else SHA).fullmatch(arguments[3])):
        raise ValueError('unsupported_control_command')
    return tuple(arguments)


def invoke(directory, arguments):
    return subprocess.run(['/usr/bin/python3', '-s', '-E', '-B', str(directory / 'stage_helper.py'), *arguments],
                          stdin=sys.stdin.buffer, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10800,
                          cwd='/', env={'PATH': '/usr/bin:/bin', 'HOME': '/root'})


def execute(arguments, root=ROOT, prepare=stage, call=invoke):
    arguments = parse(list(arguments))
    before, directory = current_record(root)
    pending = root / 'control-pending.json'
    if arguments[0] != 'deploy':
        if pending.exists() and arguments[0] != 'status':
            raise ValueError('control_switch_requires_reconciliation')
        return call(directory, arguments)
    if pending.exists():
        raise ValueError('control_switch_requires_reconciliation')
    blocked = root / 'control-blocked.json'
    if blocked.exists() and json.loads(blocked.read_text()).get('commit') == arguments[2]:
        raise ValueError('control_candidate_failed')
    policy = json.loads((root / 'policy.json').read_text())
    if (root / 'switch-pending.json').exists():
        raise ValueError('application_switch_requires_reconciliation')
    after = prepare(arguments[2], before['commit'], root)
    target = verify_record(after, root)
    record = {'before': before, 'after': after, 'application_before': policy['active'], 'phase': 'prepared'}
    save(pending, record)
    save(root / 'control-release.json', after)
    record['phase'] = 'invocation_intent'
    save(pending, record)
    result = call(target, arguments)
    policy = json.loads((root / 'policy.json').read_text())
    if (root / 'switch-pending.json').exists():
        raise ValueError('application_outcome_requires_reconciliation')
    if result.returncode == 0 and policy['active'] == arguments[2]:
        save(root / 'control-previous.json', before)
        record['phase'] = 'healthy'
    elif result.returncode != 0 and policy['active'] == record['application_before']:
        verify_record(before, root)
        save(root / 'control-release.json', before)
        save(blocked, {'commit': arguments[2]})
        record['phase'] = 'controller_rolled_back'
    else:
        raise ValueError('control_outcome_requires_reconciliation')
    save(root / 'control-last-operation.json', record)
    pending.unlink()
    sync_directory(root)
    return result


def main():
    if os.getuid() != 0:
        raise ValueError('restricted_root_entry_only')
    arguments = parse(sys.argv[1:])
    admin(ROOT, directory=True)
    lock = os.open(ROOT / 'control-entry.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = execute(arguments)
        sys.stdout.buffer.write(result.stdout)
        sys.stderr.buffer.write(result.stderr)
        return result.returncode
    finally:
        os.close(lock)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('{"error":"control_update_requires_reconciliation"}', file=sys.stderr)
        raise SystemExit(1)
