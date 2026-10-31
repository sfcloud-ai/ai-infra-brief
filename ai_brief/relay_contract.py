"""晨报与独立转发器之间的纯数据适配接口；不上传文件，也不发送消息。"""

from copy import deepcopy
from html import escape

from .contracts import validate
from .delivery_format import title
from .storage import BriefError


def _text(value):
    return escape(value, quote=False)


def prepare_relay_preview(brief, attached_files=None):
    """生成离线 Slack 事件模板，而不是可直接发送的 Slack API 请求。

    文件参数应来自将来的 Slack 上传结果；这里只提供离线契约，不能编造真实
    文件 ID、消息时间或发送者身份。接收侧继续使用原转发器的完整附件校验。
    """
    validate("brief", brief)
    files = [] if attached_files is None else deepcopy(attached_files)
    if not isinstance(files, list) or len(files) > 10:
        raise BriefError("Relay preview supports at most 10 direct image attachments")
    for file in files:
        if not isinstance(file, dict) or file.get("mimetype") not in {"image/png", "image/jpeg"}:
            raise BriefError("Relay needs PNG/JPEG Slack file metadata; SVG requires rasterization and upload first")
    lines = [title(brief["date"])]
    if brief["mode"] == "mock":
        lines += ["模拟干跑预览，不可外发。"]
    if not brief["items"]:
        lines += ["", "本次没有合格新项。"]
        if brief["mode"] == "mock":
            lines.append("本次仅使用本地模拟输入，未做真实检索。")
    for index, item in enumerate(brief["items"], 1):
        lines += ["", f"{index}. {_text(item['title'])}", _text(item["summary"]),
                  f"与当前工作的关系：{_text(item['relevance'])}"]
        if item["limitations"].strip():
            lines.append(f"选型限制：{_text(item['limitations'])}")
        lines += [f"证据：{_text(item['evidence'])}",
                  f"来源：{_text(item['source_name'])} {_text(item['source_url'])}",
                  f"发布：{item['published_at']}"]
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 65536:
        raise BriefError("Relay preview exceeds the receiver's 65536-byte text limit")
    event = {"text": text}
    if files:
        event.update(subtype="file_share", files=files)
    return {
        "schema_version": "1.0", "mode": "dry_run", "delivery_allowed": False,
        "publisher_status": "not_connected", "source_mode": brief["mode"],
        "event_template": event,
        "pending_steps": ["real_research_and_model", "png_jpeg_render_if_needed", "slack_upload_and_publish"],
    }
