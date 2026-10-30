"""候选交接适配器：沿用游标和去重账本，不改变原转发器默认启动语义。"""

import hashlib
import json


def binding(config):
    identity = {name: config[name] for name in ("team_id", "user_id", "channel_id", "sender_id")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def resume_class(module):
    class ResumeRelay(module.Relay):
        def baseline(self):
            # 此绑定只能在首次停止旧实例、备份并核对配置后写入，不能自动认领旧游标。
            if self.store.get("handoff_binding_v1") != binding(self.config):
                raise module.RelayError("handoff_binding_required", 409)
            cursor = self.store.get("cursor")
            if not isinstance(cursor, str) or not module.TS.fullmatch(cursor):
                raise module.RelayError("handoff_cursor_required", 409)
            self.connected = True

    return ResumeRelay


def assert_reconciled(store):
    # 结果未知时无法同时保证不重发和不漏发；必须查回执，不自动覆盖状态。
    for table in ("requests", "parts", "image_parts"):
        count = store.db.execute("SELECT COUNT(*) FROM " + table + " WHERE status IN ('sending','unknown')").fetchone()[0]
        if count:
            raise ValueError("unreconciled_delivery_blocks_cutover")
