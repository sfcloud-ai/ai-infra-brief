"""单向、同版本的显式发送模式迁移；仅由受限入口持部署锁调用。"""

import hashlib
import json
import re
from pathlib import Path

if __package__:
    from . import control_entry, scheduler_service
    from .release import write_json
    from .supervisor import ServiceError
else:
    import control_entry
    import scheduler_service
    from release import write_json
    from supervisor import ServiceError

RECORD = 'mode-transition.json'
VERIFIER_MAINTENANCE = Path('/opt/ai-brief-scheduler/verifier-maintenance')


def maintained_verifier(value, expected, expected_binding):
    """只接受已完成且原账本保留的管理员 verifier 维护链，不改迁移历史。"""
    def read(path):
        scheduler_service.admin_path(path)
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError('maintenance_record_too_large')
        return raw, json.loads(raw, object_pairs_hook=scheduler_service.unique)

    seen, next_binding = set(), None
    try:
        while value != expected:
            if ({k: v for k, v in value.items() if k != 'verifier_path'}
                    != {k: v for k, v in expected.items() if k != 'verifier_path'}):
                return False
            script = Path(value['verifier_path'])
            sha = script.parent.parent.name
            if (script != scheduler_service.VERIFIERS / sha / 'scripts/verify-scheduled-delivery.mjs'
                    or not re.fullmatch('[a-f0-9]{40}', sha) or sha in seen or len(seen) >= 64):
                return False
            seen.add(sha)
            root = VERIFIER_MAINTENANCE / sha
            original_raw, original = read(root / 'original.json')
            candidate_raw, candidate = read(root / 'candidate.json')
            _, prepared = read(root / 'prepared.json')
            _, activation = read(root / 'activation.json')
            old_binding, new_binding = prepared.get('old_binding'), activation.get('new_binding')
            if (candidate != value or prepared.get('reports') != sha
                    or activation.get('reports') != sha
                    or not re.fullmatch('[a-f0-9]{40}', prepared.get('application', ''))
                    or activation.get('application') != prepared['application']
                    or prepared.get('original_config_sha256') != hashlib.sha256(original_raw).hexdigest()
                    or prepared.get('candidate_config_sha256') != hashlib.sha256(candidate_raw).hexdigest()
                    or not isinstance(old_binding, str) or not re.fullmatch('[a-f0-9]{64}', old_binding)
                    or not isinstance(new_binding, str) or not re.fullmatch('[a-f0-9]{64}', new_binding)
                    or activation.get('old_binding') != old_binding
                    or next_binding is not None and next_binding != new_binding
                    or activation.get('phase') != 'complete'
                    or activation.get('original_records_preserved') is not True
                    or activation.get('application_policy_unchanged') is not True):
                return False
            value, next_binding = original, old_binding
        # Runtime binding includes the fixed verifier files, Node and UID/GID.
        # Preserve those audited opaque identities; never substitute a config hash.
        return next_binding == expected_binding
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def guard(root):
    path = Path(root) / RECORD
    if path.exists():
        record = json.loads(path.read_text())
        if record.get('phase') != 'complete':
            raise ServiceError('mode_transition_requires_forward_recovery')
        value = json.loads(scheduler_service.CONFIG.read_text(),
                           object_pairs_hook=scheduler_service.unique)
        expected = {**record['old_config'], 'automatic_delivery': 'scheduled_only'}
        if value != expected and not maintained_verifier(value, expected, record.get('new_binding')):
            raise ServiceError('migration_mode_regression')


SOURCE_FILES = ('mode_transition.py', 'stage_helper.py', 'supervisor.py', 'scheduler_service.py',
                'policy.py', 'release.py', 'ssh_entry.py', 'control_entry.py')


def verify_source(release, evidence=None, *, recovery=False):
    """首次验证当前主线和官方 CI；恢复只核已落盘的批准证据与固定源码。"""
    if recovery:
        if (not isinstance(evidence, dict) or set(evidence) != {'ci', 'files'}
                or not isinstance(evidence['files'], dict) or set(evidence['files']) != set(SOURCE_FILES)
                or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h)
                       for h in evidence['files'].values())):
            raise ServiceError('migration_source_evidence_invalid')
        proof = evidence['ci']
        if (not isinstance(proof, dict) or set(proof) != {'commit', 'main', 'run_id', 'run_attempt'}
                or proof['commit'] != release or not isinstance(proof['main'], str)
                or not re.fullmatch('[0-9a-f]{40}', proof['main'])
                or any(type(proof[k]) is not int or proof[k] <= 0 for k in ('run_id', 'run_attempt'))):
            raise ServiceError('migration_ci_evidence_invalid')
        tree = None
    else:
        proof = control_entry.trusted_candidate(release, release)
        tree = control_entry.source_tree(release, control_entry.api)
    files = {}
    for name in SOURCE_FILES:
        path = Path(__file__).resolve().parent / name
        scheduler_service.admin_path(path)
        data = path.read_bytes()
        files[name] = hashlib.sha256(data).hexdigest()
        if recovery:
            matches = files[name] == evidence['files'][name]
        else:
            matches = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() == tree.get(name)
        if not matches:
            raise ServiceError('migration_controller_source_changed')
    if not recovery and control_entry.trusted_candidate(release, release) != proof:
        raise ServiceError('migration_ci_changed')
    return {'ci': proof, 'files': files}


def inventory(driver, release, allowed):
    """严格查询两容器；Docker 错误不等价于容器不存在。"""
    found = None
    for name in (driver.live['container'], driver.live['legacy_container']):
        result = driver.command(['container', 'ls', '--all', '--no-trunc', '--filter',
                                 'name=^/' + re.escape(name) + '$', '--format', '{{.ID}}'])
        ids = result.stdout.decode().split()
        if len(ids) > 1:
            raise ServiceError('migration_container_inventory_ambiguous')
        if not ids:
            continue
        item = json.loads(driver.command(['inspect', ids[0]]).stdout)[0]
        state = item['State']
        if any(type(state.get(key)) is not bool for key in ('Running', 'Restarting', 'Paused')):
            raise ServiceError('migration_container_state_unknown')
        if name == driver.live['legacy_container']:
            if any(state[key] for key in ('Running', 'Restarting', 'Paused')):
                raise ServiceError('migration_legacy_sender_present')
            continue
        labels = item['Config'].get('Labels') or {}
        if (item['Id'] != ids[0] or labels.get('sfcloud.ai.service') != 'ai-infra-brief'
                or labels.get('sfcloud.ai.release') != release
                or labels.get(scheduler_service.LABEL) not in allowed
                or state['Restarting'] or state['Paused']):
            raise ServiceError('migration_container_identity_changed')
        found = item
    return found


def offline_check(driver, release):
    driver.one_shot(['check-state', '--root', '/relay', '--code', driver.code(release)],
                    readonly_state=True, runtime=driver.runtime(release))


def migrate(command, config, root, policy, driver_factory, source_check=verify_source,
            inspect=inventory):
    """不回滚配置或旧自动队列。任何未知结果保留记录，显式重试同一命令。"""
    root = Path(root)
    _, sequence, release, old_binding = command
    sequence = int(sequence)
    if (root / 'switch-pending.json').exists():
        raise ServiceError('application_switch_requires_reconciliation')
    accepted = policy.value.get('accepted') or {}
    if (policy.value['sequence'] != sequence or policy.value['active'] != release
            or accepted.get('commit') != release or policy.value.get('rollback_block')):
        raise ServiceError('migration_active_identity_changed')
    ready = Path(config['root']) / 'releases' / release / 'ready.json'
    if (ready.is_symlink() or json.loads(ready.read_text()) !=
            {'commit': release, 'bundle_sha256': accepted.get('checksum')}):
        raise ServiceError('migration_release_identity_changed')
    path = root / RECORD
    record = None
    if path.exists():
        scheduler_service.admin_path(path)
        record = json.loads(path.read_text())
        if (record.get('version') != 1 or record.get('sequence') != sequence
                or record.get('release') != release or record.get('old_binding') != old_binding
                or record.get('phase') not in ('prepared', 'stopped', 'configured', 'activation_intent', 'complete')):
            raise ServiceError('migration_record_identity_changed')
        source_check(release, record.get('source'), recovery=True)
    else:
        source = source_check(release)
    driver = driver_factory(config, root)
    scheduler = driver.scheduler
    if scheduler is None:
        raise ServiceError('migration_scheduler_required')
    if record is not None:
        old = record['old_config']
        new = {**old, 'automatic_delivery': 'scheduled_only'}
        if ('automatic_delivery' in old or scheduler.binding_for(old) != old_binding
                or scheduler.binding_for(new) != record['new_binding']
                or scheduler.value not in (old, new)):
            raise ServiceError('migration_scope_changed')
        if record['phase'] != 'prepared' and scheduler.value == old and record['phase'] != 'stopped':
            raise ServiceError('migration_mode_regression')
    else:
        if scheduler.binding != old_binding or 'automatic_delivery' in scheduler.value:
            raise ServiceError('migration_original_binding_required')
        driver.preflight(release)
        driver.preflight_rollback(release)
        scheduler.preflight(driver, release, require_scheduled_only=True)
        driver.assert_scheduler_binding()
        current = inspect(driver, release, {old_binding})
        if current is None or current['State']['Running'] is not True:
            raise ServiceError('migration_active_container_required')
        old = scheduler.value
        new = {**old, 'automatic_delivery': 'scheduled_only'}
        record = {'version': 1, 'sequence': sequence, 'release': release, 'old_binding': old_binding,
                  'new_binding': scheduler.binding_for(new), 'old_config': old,
                  'old_container': current['Id'], 'phase': 'prepared', 'source': source}
        write_json(path, record)
    new_binding = record['new_binding']
    if record['phase'] == 'prepared':
        if scheduler.value != old:
            raise ServiceError('migration_config_changed_before_stop')
        current = inspect(driver, release, {old_binding})
        if current is None or current['Id'] != record['old_container']:
            raise ServiceError('migration_old_container_changed')
        driver.assert_scheduler_binding()
        if current['State']['Running']:
            driver.quiesce(release)
        current = inspect(driver, release, {old_binding})
        if current is None or current['Id'] != record['old_container'] or current['State']['Running']:
            raise ServiceError('migration_old_stop_unconfirmed')
        # 断电可能发生于 stop 后、阶段落盘前；重启授权必须已经撤销。
        if current['HostConfig']['RestartPolicy']['Name'] != 'no':
            raise ServiceError('migration_restart_not_revoked')
        for name in ('active.json', 'permit.json'):
            if json.loads((driver.activation / name).read_text()).get('release') is not None:
                raise ServiceError('migration_activation_not_revoked')
        offline_check(driver, release)
        record['phase'] = 'stopped'
        write_json(path, record)
    if record['phase'] == 'stopped':
        current = inspect(driver, release, {old_binding})
        if current is None or current['Id'] != record['old_container'] or current['State']['Running']:
            raise ServiceError('migration_old_stop_unconfirmed')
        offline_check(driver, release)
        # 原配置仅允许添加此字段；write_json 提供 fsync 和原子替换。
        if scheduler.value == old:
            scheduler_service.admin_path(scheduler_service.CONFIG)
            control_entry.atomic(scheduler_service.CONFIG,
                                 (json.dumps(new, sort_keys=True) + '\n').encode(), 0o644)
        driver = driver_factory(config, root)
        scheduler = driver.scheduler
        if scheduler.value != new or scheduler.binding != new_binding:
            raise ServiceError('migration_new_binding_changed')
        driver.preflight(release)
        record['phase'] = 'configured'
        write_json(path, record)
    if scheduler.value != new or scheduler.binding != new_binding:
        raise ServiceError('migration_mode_regression')
    if record['phase'] == 'complete':
        current = inspect(driver, release, {new_binding})
        if current is None or not current['State']['Running']:
            raise ServiceError('migration_completed_sender_missing')
        driver.wait_healthy(release)
        return {'status': 'scheduled_only', 'commit': release, 'sequence': sequence}
    current = inspect(driver, release, {old_binding, new_binding})
    if current is not None and current['Config']['Labels'][scheduler_service.LABEL] == old_binding:
        if (current['Id'] != record['old_container'] or current['State']['Running']
                or record['phase'] != 'configured'):
            raise ServiceError('migration_old_sender_reappeared')
    if current is None or not current['State']['Running']:
        if record['phase'] == 'activation_intent':
            # 已可能发送的容器不强制删除；需人工对账，不能猜测发送结果。
            raise ServiceError('migration_activation_outcome_requires_reconciliation')
        offline_check(driver, release)
        driver.start_paused(release)
    driver.assert_scheduler_binding()
    current = inspect(driver, release, {new_binding})
    if current is None or not current['State']['Running']:
        raise ServiceError('migration_new_sender_missing')
    if record['phase'] == 'configured':
        # docker run -d 只确认进程启动，不保证控制 socket 已监听。
        # 仅尚未尝试激活的实例使用既有有界 readiness 等待；未知激活结果不走此分支。
        driver.wait_ready(release)
    status = driver.status(release)
    if status.get('phase') == 'paused':
        driver.wait_ready(release)
        record['phase'] = 'activation_intent'
        write_json(path, record)
        driver.activate(release)
    elif status.get('phase') != 'active' or record['phase'] != 'activation_intent':
        raise ServiceError('migration_activation_state_unknown')
    driver.wait_healthy(release)
    record['phase'] = 'complete'
    write_json(path, record)
    return {'status': 'scheduled_only', 'commit': release, 'sequence': sequence}
