"""各篇子标题、文章 URL 与单张总海报的离线契约；不发送消息。"""

from copy import deepcopy
import hashlib
import json
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


def prepare_article_notice_preview(report_bytes, report_base_url, poster, *, publication_commit):
    """复用 report.json 的子标题及链接；离线计划不授予投递权限。"""
    if find_spec("ai_brief_relay"):
        from ai_brief_relay import notice_format
    else:
        from integrations.slack_wecom import notice_format
    if (not isinstance(report_bytes, bytes) or not 0 < len(report_bytes) < 128 * 1024
            or not isinstance(poster, dict) or poster.get("mimetype") not in {"image/png", "image/jpeg"}):
        raise BriefError("Expected exact report bytes and one direct PNG/JPEG poster")
    try:
        report = json.loads(report_bytes.decode("utf-8"))
        manifest = notice_format.build_manifest(report, report_base_url, hashlib.sha256(report_bytes).hexdigest(), publication_commit)
    except (ValueError, TypeError, KeyError):
        raise BriefError("Invalid article notice manifest or byte budget") from None
    return {"schema_version": "1.0", "mode": "dry_run", "delivery_allowed": False,
            "publisher_status": "not_connected", "publication_status": "not_checked",
            "notice_manifest": manifest,
            "event_template": {"text": manifest["text"], "subtype": "file_share", "files": [deepcopy(poster)]}}


def check_received_notice(plan, message, *, channel_id, sender_id, team_id, read_context, notice_connector=None, actual_poster_bytes=None):
    """复用独立转发器安装包中的同一套通知校验函数。"""
    if find_spec("ai_brief_relay"):
        from ai_brief_relay.notice_check import check_received_notice as check, NoticeCheckError
    else:
        from integrations.slack_wecom.notice_check import check_received_notice as check, NoticeCheckError
    try:
        return check(plan, message, channel_id=channel_id, sender_id=sender_id, team_id=team_id,
                     read_context=read_context, notice_connector=notice_connector,
                     actual_poster_bytes=actual_poster_bytes)
    except NoticeCheckError as error:
        raise BriefError(str(error)) from error
