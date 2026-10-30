"""仅实现模拟适配器；不联网，也不读取环境中的凭据。"""

from typing import Protocol

from .contracts import validate
from .storage import BriefError, digest, json_text, read_json, write_json


class ResearchAdapter(Protocol):
    def research(self, context: dict, prompt: str) -> dict: ...


class EditorAdapter(Protocol):
    def edit(self, selected: list, context: dict, prompt: str) -> dict: ...


class DeliveryAdapter(Protocol):
    def send(self, key: str, payload: dict) -> str: ...


class MockResearchAdapter:
    def __init__(self, fixture, project):
        self.data = read_json(fixture)
        validate("candidates", self.data, project)
        if self.data["mode"] != "mock" or any(x["verification"] != "mock_unverified" for x in self.data["items"]):
            raise BriefError("Mock fixtures must be mode=mock and every item mock_unverified")

    def research(self, context, prompt):
        # 不把确定性读取伪装成模型执行了提示词。
        return self.data


class MockEditorAdapter:
    def edit(self, selected, context, prompt):
        return {
            "schema_version": "1.0", "mode": "mock", "date": context["date"],
            "timezone": "Asia/Shanghai", "as_of": context["as_of"],
            "status": "ready" if selected else "zero_items",
            "window_hours": context["window_hours"], "items": selected,
            "notes": context["notes"], "history_files": context["history_files"],
            "warning": "模拟干跑：未联网、未调用模型、未核验来源，不可作为真实晨报发送。",
            "adapter_status": {"research": "mock_fixture_only", "editor": "mock_deterministic_only", "delivery": "not_connected"},
        }


class MockDeliveryAdapter:
    """仅写入确定性的本地回执，绝不发送真实消息。"""
    def __init__(self, outbox):
        self.outbox = outbox

    def send(self, key, payload):
        receipt = "mock:" + digest(key + json_text(payload))[:24]
        path = self.outbox / f"{key}.json"
        data = {"mode": "simulation", "key": key, "receipt": receipt, "payload": payload}
        if path.exists() and read_json(path) != data:
            raise BriefError("Mock receipt key already exists with different content")
        if not path.exists():
            write_json(path, data)
        return receipt


def require_mock(name):
    if name != "mock":
        raise BriefError("Live research/model/Slack adapters are NOT CONNECTED; no credentials are read and nothing was sent")
