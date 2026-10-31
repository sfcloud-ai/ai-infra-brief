"""子标题、报告 URL 与单张信息海报的离线契约；不发送消息。"""

from copy import deepcopy
from datetime import date
from importlib.util import find_spec
from urllib.parse import urlsplit

from .storage import BriefError


def title(day):
    """固定接收器认可的首行前缀。"""
    try:
        valid = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
    except ValueError:
        valid = False
    if not valid:
        raise BriefError("Expected ISO report date")
    return f"AI Infra 晨报 · {day}"


def prepare_notice_preview(day, subtitle, report_url, poster):
    """生成单条顶层图文模板，不核验站点上线，也不授予投递权限。"""
    heading = title(day)
    if (not isinstance(subtitle, str) or not subtitle.strip() or len(subtitle.encode()) > 180
            or any(char in subtitle for char in "\r\n<>")):
        raise BriefError("Expected a short, single-line report subtitle")
    if not isinstance(report_url, str):
        raise BriefError("Expected a report URL string")
    try:
        parsed = urlsplit(report_url)
        valid = (isinstance(report_url, str) and parsed.scheme == "https" and parsed.hostname
                 and not parsed.username and not parsed.password and parsed.port in (None, 443)
                 and not parsed.query and not parsed.fragment
                 and not any(char.isspace() or ord(char) < 32 or char in "<>&" for char in report_url))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise BriefError("Expected a permanent HTTPS report URL without credentials or query parameters")
    if not isinstance(poster, dict) or poster.get("mimetype") not in {"image/png", "image/jpeg"}:
        raise BriefError("Expected one direct PNG/JPEG information poster")
    text = f"{heading} · {subtitle.strip()}\n\n{report_url}"
    if len(text.encode()) > 1900:
        raise BriefError("Notice exceeds relay single-part budget; shorten the subtitle or report URL")
    return {"schema_version": "1.0", "mode": "dry_run", "delivery_allowed": False,
            "publisher_status": "not_connected", "publication_status": "not_checked",
            "event_template": {"text": text, "subtype": "file_share", "files": [deepcopy(poster)]}}


def check_received_notice(plan, message, *, channel_id, sender_id, team_id, read_context, notice_connector=None):
    """核对实际读取的 Slack 顶层图文；不把匹配规则当作企微送达回执。"""
    if find_spec("ai_brief_relay"):
        from ai_brief_relay import relay
        from ai_brief_relay.images import select_images
    else:
        from integrations.slack_wecom import relay
        from integrations.slack_wecom.images import select_images

    if (not all(isinstance(value, str) and value for value in (channel_id, sender_id, team_id))
            or read_context != {"channel_id": channel_id, "team_id": team_id}):
        raise BriefError("Authenticated Slack read context differs from expected destination")
    template = plan.get("event_template", {})
    if not isinstance(message, dict) or not isinstance(template.get("text"), str):
        raise BriefError("Expected a notice template and received Slack message")
    ts = message.get("ts")
    if notice_connector is not None:
        try:
            if notice_connector.get("user_id") != sender_id:
                raise relay.RelayError("untrusted_notice")
            notice = relay.connector_notice(message, notice_connector, team_id, channel_id)
        except (relay.RelayError, AttributeError) as error:
            raise BriefError("Slack connector notice contract failed") from error
        wanted = template.get("files", [])
        if (notice["text"] != relay.plain_text(template["text"])
                or len(wanted) != 1 or not wanted[0].get("id")
                or notice["images"][0]["file_id"] != wanted[0]["id"]):
            raise BriefError("Slack connector title, URL or information poster differs from plan")
        return {"slack_status": "verified", "wecom_status": "not_checked", "publication_status": "not_checked",
                "receipt": {"channel": channel_id, "ts": ts, "relay_request_id": "auto:" + ts}}
    receiver = relay.Relay(None, None, None, {"sender_id": sender_id})
    if (message.get("channel", channel_id) != channel_id or message.get("team", team_id) != team_id
            or not isinstance(ts, str) or not relay.TS.fullmatch(ts)
            or message.get("hidden") or message.get("type", "message") != "message"
            or message.get("subtype") != "file_share" or not receiver.is_brief(message)):
        raise BriefError("Slack identity, title or top-level file-share contract failed")
    expected = relay.plain_text(template["text"])
    blocks = message.get("blocks", [])
    if message.get("attachments") or (isinstance(blocks, list) and any(
            isinstance(block, dict) and "accessory" in block for block in blocks)):
        raise BriefError("Slack notice contains unsupported visible accessories or attachments")
    if "blocks" in message and message["blocks"] != []:
        # 空 fallback 强制检查 blocks 自身，不能用 fallback 冒充可见正文。
        if not isinstance(message["blocks"], list) or relay.message_text({**message, "text": ""}) != expected:
            raise BriefError("Slack visible blocks are incomplete or unverifiable")
    actual = relay.message_text(message)
    if actual != expected or len(relay.chunks(actual)) != 1:
        raise BriefError("Slack visible subtitle or complete URL differs from the single-part plan")
    files, wanted = message.get("files"), template.get("files", [])
    selected = select_images(message, team_id)
    if (not isinstance(files, list) or len(files) != 1 or len(wanted) != 1 or not wanted[0].get("id")
            or len(selected) != 1 or "error" in selected[0] or selected[0].get("file_id") != wanted[0]["id"]):
        raise BriefError("Slack direct information poster differs from plan")
    return {"slack_status": "verified", "wecom_status": "not_checked", "publication_status": "not_checked",
            "receipt": {"channel": channel_id, "ts": ts, "relay_request_id": "auto:" + ts}}
