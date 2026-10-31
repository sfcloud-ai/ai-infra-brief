"""交接适配器：沿用游标和去重账本，不改变旧入口默认启动语义。"""

from collections import deque
import decimal
import hashlib
import json
import os


class RetryableReadError(Exception):
    """仅表示尚未开始投递的历史读取失败，可保留游标稍后重试。"""



def binding(config):
    identity = {name: config[name] for name in ("team_id", "user_id", "channel_id", "sender_id")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def resume_class(module):
    class ResumeRelay(module.Relay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pending_messages = deque()

        def baseline(self):
            # 此绑定只能在首次停止旧实例、备份并核对配置后写入，不能自动认领旧游标。
            if self.store.get("handoff_binding_v1") != binding(self.config):
                raise module.RelayError("handoff_binding_required", 409)
            cursor = self.store.get("cursor")
            if not isinstance(cursor, str) or not module.TS.fullmatch(cursor):
                raise module.RelayError("handoff_cursor_required", 409)
            self.connected = True

        def poll(self):
            if not self.connected:
                self.baseline()
            if not self.pending_messages:
                try:
                    self.pending_messages.extend(self.slack.newer(self.store.get("cursor")))
                except module.RelayError as error:
                    if error.code in {"slack_request_failed", "slack_api_rejected"}:
                        raise RetryableReadError("history_read_retry") from None
                    raise
            # 批次只读一次；每轮至多处理一条，让控制请求在消息之间获得执行机会。
            while self.pending_messages:
                message = self.pending_messages[0]
                timestamp = message.get("ts", "")
                if not module.TS.fullmatch(timestamp):
                    raise module.RelayError("invalid_slack_timestamp", 502)
                if decimal.Decimal(timestamp) <= decimal.Decimal(self.store.get("cursor")):
                    self.pending_messages.popleft()
                    continue
                if self.is_brief(message):
                    text = getattr(self, "delivery_text", module.message_text)(message)
                    notice_key = getattr(self, "notice_key", lambda value: None)(message)
                    extra = {"notice_key": notice_key} if notice_key is not None else {}
                    self.deliver("auto:" + timestamp, {"mode": "auto", "ts": timestamp},
                                 text, timestamp, "auto", os.getuid(),
                                 image_plans=self.image_plans(message), **extra)
                self.store.set("cursor", timestamp)
                self.pending_messages.popleft()
                return True
            return False

    return ResumeRelay


def assert_reconciled(store):
    # 结果未知时无法同时保证不重发和不漏发；必须查回执，不自动覆盖状态。
    for table in ("requests", "parts", "image_parts"):
        count = store.db.execute("SELECT COUNT(*) FROM " + table + " WHERE status IN ('sending','unknown')").fetchone()[0]
        if count:
            raise ValueError("unreconciled_delivery_blocks_cutover")
