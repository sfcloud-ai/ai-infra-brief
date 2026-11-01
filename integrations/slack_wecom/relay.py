"""私有的单工作区 Slack → 企业微信转发器，仅使用标准库。"""
from __future__ import annotations

import argparse
import base64
import contextlib
import decimal
from datetime import date
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# /control 与应用 wheel 分开安装；禁止把 wheel 更新误认为控制层已升级。
if __name__ == "managed_relay" and "article_notice_read_v1" not in globals().get("CONTROL_CAPABILITIES", ()):
    raise ValueError("administrator_control_upgrade_required")

if __package__:
    from .images import ImageError, MAX_IMAGE_BYTES, image_url, select_images, validate_image
    from . import notice_format
else:
    from images import ImageError, MAX_IMAGE_BYTES, image_url, select_images, validate_image
    import notice_format

NoticeReadError = notice_format.NoticeReadError

TITLE = re.compile(r"^\*{0,2}(?:(?:20\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日\s*)?AI\s+Infra\s+晨报(?=[\s：:*]|$)", re.I)
TS = re.compile(r"^[0-9]{10,12}\.[0-9]{6}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9:_-]{1,120}$")
MAX_BODY = 65536
MAX_SLACK_REPLY = 1048576
IMAGE_DOWNLOAD_TIMEOUT = 30


class RelayError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def secure_dir(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RelayError("unsafe_directory", 500)


def secure_open(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(fd)
        raise RelayError("unsafe_file", 500)
    return fd


def secure_read(path):
    fd = secure_open(path)
    with os.fdopen(fd, "rb") as stream:
        value = stream.read(MAX_BODY + 1)
        if len(value) > MAX_BODY:
            raise RelayError("file_too_large", 500)
        return value


def load_config(root):
    root = Path(root)
    if str(root.resolve()) != str(root.absolute()):
        raise RelayError("symlinked_root", 500)
    for path in (root, root / "secrets", root / "state", root / "run"):
        secure_dir(path)
    config = json.loads(secure_read(root / "config.json"))
    required = {"team_id", "user_id", "channel_id", "sender_id", "poll_seconds"}
    if (not required <= set(config) or set(config) - required - {"forward_images", "notice_connector"}
            or not 30 <= config["poll_seconds"] <= 3600
            or type(config.get("forward_images", False)) is not bool):
        raise RelayError("invalid_config", 500)
    for key, prefix in (("team_id", "T"), ("user_id", "U"),
                        ("channel_id", "D"), ("sender_id", "U")):
        if not re.fullmatch(prefix + r"[A-Z0-9]+", config[key]):
            raise RelayError("invalid_config", 500)
    if "notice_connector" in config:
        profile = validate_notice_connector(config["notice_connector"])
        if (not config.get("forward_images") or profile["user_id"] != config["user_id"]
                or profile["user_id"] == config["sender_id"]):
            raise RelayError("invalid_notice_connector", 500)
    return config


def validate_notice_connector(profile):
    """固定一个用户授权的 App，同时保留原晨报发送者。"""
    keys = {"user_id", "app_id", "attribution_text", "report_base_url", "not_before"}
    if not isinstance(profile, dict) or set(profile) != keys:
        raise RelayError("invalid_notice_connector", 500)
    if (not isinstance(profile["user_id"], str) or not re.fullmatch(r"U[A-Z0-9]+", profile["user_id"])
            or not isinstance(profile["app_id"], str) or not re.fullmatch(r"A[A-Z0-9]+", profile["app_id"])
            or not isinstance(profile["not_before"], str) or not TS.fullmatch(profile["not_before"])
            or not isinstance(profile["attribution_text"], str) or len(profile["attribution_text"]) > 200
            or not re.fullmatch(r"\*[^*\r\n<>]+\* <@U[A-Z0-9]+>", profile["attribution_text"])):
        raise RelayError("invalid_notice_connector", 500)
    base = profile["report_base_url"]
    try:
        u = urllib.parse.urlsplit(base)
        valid = (isinstance(base, str) and u.scheme == "https" and u.hostname
                 and not u.username and not u.password and u.port in (None, 443)
                 and not u.query and not u.fragment and base.endswith("/")
                 and not any(c.isspace() or ord(c) < 32 or c in "<>&\\" for c in base))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise RelayError("invalid_notice_connector", 500)
    return profile


def notice_url_day(url, profile):
    if not isinstance(url, str):
        raise RelayError("notice_url_rejected")
    match = re.fullmatch(re.escape(profile["report_base_url"]) + r"(\d{4}-\d{2}-\d{2})/r([1-9]\d*)/", url)
    try:
        valid = match and date.fromisoformat(match[1]).isoformat() == match[1]
    except ValueError:
        valid = False
    if not valid:
        raise RelayError("notice_url_rejected")
    return match[1]


def _connector_candidate(message, profile, team_id, channel_id):
    """校验经认证读取的原始历史，仅返回通过校验且移除工具归属的正文。"""
    validate_notice_connector(profile)
    if (not isinstance(message, dict) or message.get("user") != profile["user_id"]
            or message.get("app_id") != profile["app_id"] or message.get("bot_id") is not None
            or message.get("type") != "message" or message.get("subtype") not in (None, "file_share")
            or "edited" in message or message.get("hidden")
            or message.get("team", team_id) != team_id or message.get("channel", channel_id) != channel_id
            or not isinstance(message.get("ts"), str) or not TS.fullmatch(message["ts"])
            or message.get("thread_ts", message["ts"]) != message["ts"]
            or decimal.Decimal(message["ts"]) <= decimal.Decimal(profile["not_before"])
            or message.get("attachments") not in (None, [])):
        raise RelayError("untrusted_notice")
    blocks = message.get("blocks")
    if (not isinstance(blocks, list) or len(blocks) != 2
            or any(not isinstance(b, dict) or set(b) - {"type", "block_id", "elements"} for b in blocks)
            or blocks[0].get("type") != "rich_text" or blocks[1].get("type") != "context"):
        raise RelayError("notice_blocks_rejected")
    sections, footer = blocks[0].get("elements"), blocks[1].get("elements")
    if (not isinstance(sections, list) or len(sections) != 1 or not isinstance(sections[0], dict)
            or set(sections[0]) != {"type", "elements"} or sections[0]["type"] != "rich_text_section"
            or footer != [{"type": "mrkdwn", "text": profile["attribution_text"], "verbatim": False}]):
        raise RelayError("notice_blocks_rejected")
    elements = sections[0]["elements"]
    first_text = ""
    if isinstance(elements, list):
        for node in elements:
            if not isinstance(node, dict) or node.get("type") != "text" or not isinstance(node.get("text"), str):
                break
            first_text += node["text"]
    if re.match(r"^AI Infra 晨报 · \d{4}-\d{2}-\d{2}\n\n", first_text):
        return _article_candidate(message, profile, team_id, elements)
    if (not isinstance(elements, list) or len(elements) not in (2, 3)
            or any(not isinstance(e, dict) or set(e) != {"type", "text"} or e["type"] != "text"
                   or not isinstance(e["text"], str) for e in elements[:-1])):
        raise RelayError("notice_blocks_rejected")
    prefix = "".join(e["text"] for e in elements[:-1])
    link = elements[-1]
    if (not isinstance(link, dict) or link.get("type") != "link"
            or set(link) - {"type", "url", "text", "from_llm", "truncated"}
            or any(k in link and type(link[k]) is not bool for k in ("from_llm", "truncated"))):
        raise RelayError("notice_link_rejected")
    url = link.get("url")
    day = notice_url_day(url, profile)
    label = link.get("text", url)
    if not isinstance(label, str) or label not in {url, url.rstrip("/"), url.removeprefix("https://"), url.removeprefix("https://").rstrip("/")}:
        raise RelayError("notice_link_label_rejected")
    if not prefix.endswith("\n\n") or "\n" in prefix[:-2] or "\r" in prefix:
        raise RelayError("notice_title_rejected")
    heading = prefix[:-2]
    marker = "AI Infra 晨报 · " + day + " · "
    if (not heading.startswith(marker) or not heading[len(marker):].strip()
            or len(heading[len(marker):].encode()) > 180 or any(c in heading for c in "<>")):
        raise RelayError("notice_title_rejected")
    core = prefix + url
    fallback = message.get("text")
    if isinstance(fallback, str):
        fallback = fallback.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    allowed = {core, prefix + "<" + url + ">", prefix + "<" + url + "|" + label + ">"}
    if (not isinstance(fallback, str) or fallback not in {value + " " + profile["attribution_text"] for value in allowed}
            or len(core.encode()) > 1900):
        raise RelayError("notice_fallback_rejected")
    files = message.get("files")
    if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
            or files[0].get("user") != profile["user_id"] or files[0].get("user_team") != team_id):
        raise RelayError("notice_poster_owner_rejected")
    plans = select_images(message, team_id)
    if len(plans) != 1 or "error" in plans[0]:
        raise RelayError("notice_poster_rejected")
    return {"text": core, "url": url, "images": plans}




def _article_candidate(message, profile, team_id, elements):
    """先核对精确可见文字与链接节点，再读取发布清单。"""
    if not isinstance(elements, list) or not 2 <= len(elements) <= 256:
        raise RelayError("notice_blocks_rejected")
    core, links, fallback_parts = [], [], []
    for node in elements:
        if not isinstance(node, dict):
            raise RelayError("notice_blocks_rejected")
        if node.get("type") == "text":
            if set(node) != {"type", "text"} or not isinstance(node["text"], str):
                raise RelayError("notice_blocks_rejected")
            core.append(node["text"])
            fallback_parts.append([node["text"]])
        elif node.get("type") == "link":
            if (set(node) - {"type", "url", "text", "from_llm", "truncated"}
                    or any(k in node and type(node[k]) is not bool for k in ("from_llm", "truncated"))):
                raise RelayError("notice_link_rejected")
            url, label = node.get("url"), node.get("text", node.get("url"))
            if not isinstance(url, str) or not isinstance(label, str) or label not in {
                    url, url.rstrip("/"), url.removeprefix("https://"), url.removeprefix("https://").rstrip("/")}:
                raise RelayError("notice_link_label_rejected")
            core.append(url)
            links.append(url)
            fallback_parts.append([url, "<" + url + ">", "<" + url + "|" + label + ">"])
        else:
            raise RelayError("notice_blocks_rejected")
    text = "".join(core)
    try:
        identity = notice_format.parse_article_text(text, profile["report_base_url"])
        if links != [article["url"] for article in identity["articles"]]:
            raise ValueError("links_not_visible")
    except (TypeError, ValueError):
        raise RelayError("notice_articles_rejected") from None
    fallback = message.get("text")
    if not isinstance(fallback, str):
        raise RelayError("notice_fallback_rejected")
    fallback = fallback.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    suffix = " " + profile["attribution_text"]
    if not fallback.endswith(suffix):
        raise RelayError("notice_fallback_rejected")
    remaining = fallback[:-len(suffix)]
    for options in fallback_parts:
        match = next((value for value in sorted(options, key=len, reverse=True) if remaining.startswith(value)), None)
        if match is None:
            raise RelayError("notice_fallback_rejected")
        remaining = remaining[len(match):]
    if remaining:
        raise RelayError("notice_fallback_rejected")
    files = message.get("files")
    if (not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict)
            or files[0].get("user") != profile["user_id"] or files[0].get("user_team") != team_id):
        raise RelayError("notice_poster_owner_rejected")
    plans = select_images(message, team_id)
    if len(plans) != 1 or "error" in plans[0]:
        raise RelayError("notice_poster_rejected")
    return {"text": text, "url": identity["index_url"], "articles": identity["articles"],
            "images": plans, "format": "article_list_v1"}


def _bind_notice_manifest(candidate, manifest, profile):
    if candidate.get("format") == "article_list_v1":
        try:
            notice_format.match_manifest(candidate, manifest, profile["report_base_url"])
        except (TypeError, ValueError, KeyError):
            raise RelayError("notice_manifest_rejected") from None
        candidate["images"][0]["expected_sha256"] = manifest["poster"]["sha256"]
    return candidate


def connector_notice(message, profile, team_id, channel_id, *, notice_manifest=None):
    """纯校验函数：分项通知须提供独立获得的发布清单。"""
    return _bind_notice_manifest(_connector_candidate(message, profile, team_id, channel_id), notice_manifest, profile)


def load_secrets(root):
    result = json.loads(secure_read(Path(root) / "secrets" / "credentials.json"))
    return validate_credentials(result)


def validate_credentials(result):
    if set(result) != {"slack_user_token", "wecom_webhook"}:
        raise RelayError("invalid_credentials", 500)
    if not re.fullmatch(r"xoxp-[A-Za-z0-9-]+", result["slack_user_token"]):
        raise RelayError("expected_user_token", 500)
    url = urllib.parse.urlsplit(result["wecom_webhook"])
    query = urllib.parse.parse_qs(url.query, strict_parsing=True)
    if (url.scheme != "https" or url.netloc != "qyapi.weixin.qq.com"
            or url.path != "/cgi-bin/webhook/send" or url.fragment
            or set(query) != {"key"} or len(query["key"]) != 1
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", query["key"][0])):
        raise RelayError("invalid_webhook", 500)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RelayError("redirect_rejected", 502)


def opener():
    # 不得通过继承的 HTTP 代理或重定向发送这些凭据。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


@contextlib.contextmanager
def image_download_timeout():
    """在服务主线程设置硬截止时间，覆盖连接、响应头和阻塞读取。"""
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise ImageError("image_timer_already_in_use")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise ImageError("image_download_timed_out")

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, IMAGE_DOWNLOAD_TIMEOUT)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Slack:
    def __init__(self, token, config):
        self.token, self.config, self.http = token, config, opener()
        self.images_authorized = False

    def call(self, method, params=None):
        url = "https://slack.com/api/" + method
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
        try:
            with self.http.open(request, timeout=20) as response:
                raw = response.read(MAX_SLACK_REPLY + 1)
                if len(raw) > MAX_SLACK_REPLY:
                    raise RelayError("slack_response_too_large", 502)
                body = json.loads(raw)
                scopes = response.headers.get("x-oauth-scopes", "")
        except Exception:
            # 不向外传播 HTTP 异常，其中可能包含敏感 URL。
            raise RelayError("slack_request_failed", 502) from None
        if body.get("ok") is not True:
            raise RelayError("slack_api_rejected", 502)
        return body, scopes

    def verify(self):
        self.images_authorized = False
        body, scopes = self.call("auth.test")
        if (body.get("team_id") != self.config["team_id"]
                or body.get("user_id") != self.config["user_id"] or body.get("bot_id")):
            raise RelayError("wrong_slack_identity", 403)
        granted = {s.strip() for s in scopes.split(",") if s.strip()}
        # Slack 可能在用户 OAuth 授权中隐式包含 identify；
        # 它属于基础身份元数据，不是额外的消息权限。
        required = {"im:history"}
        if self.config.get("forward_images", False):
            required.add("files:read")
        if not required <= granted or granted - required - {"identify"}:
            raise RelayError("unexpected_slack_scopes", 403)
        self.images_authorized = "files:read" in required

    def download_image(self, plan):
        if not self.config.get("forward_images", False) or not self.images_authorized:
            raise ImageError("image_scope_not_verified")
        try:
            url = image_url(plan["url"], self.config["team_id"], plan["file_id"])
            size, mime = plan["size"], plan["mime"]
            if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES:
                raise ImageError("image_size_rejected")
            if mime not in ("image/png", "image/jpeg"):
                raise ImageError("unsupported_image_format")
            request = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + self.token, "Accept-Encoding": "identity"})
            deadline = time.monotonic() + 30
            # 请求器拒绝所有重定向，并忽略环境中的代理设置。
            with image_download_timeout(), self.http.open(request, timeout=20) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ImageError("image_download_rejected")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if encoding != "identity" or content_type not in (mime, "application/octet-stream"):
                    raise ImageError("image_response_type_rejected")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) != size):
                    raise ImageError("image_size_mismatch")
                buffer = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise ImageError("image_download_timed_out")
                    chunk = response.read(min(65536, MAX_IMAGE_BYTES + 1 - len(buffer)))
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer) > MAX_IMAGE_BYTES or len(buffer) > size:
                        raise ImageError("image_size_rejected")
                data = bytes(buffer)
                if len(data) < size:
                    # 合法响应头后中途 EOF 是未完成读取，不等同于完整内容身份不符。
                    raise ImageError("image_download_incomplete")
                if len(data) != size:
                    raise ImageError("image_size_mismatch")
                validate_image(data, mime)
                return data
        except ImageError:
            raise
        except Exception:
            raise ImageError("image_download_failed") from None

    def history(self, **params):
        body, _ = self.call("conversations.history", {"channel": self.config["channel_id"], **params})
        return body

    def latest(self):
        messages = self.history(limit=1).get("messages", [])
        return messages[0]["ts"] if messages else format(decimal.Decimal(time.time_ns()) / 10**9, ".6f")

    def newer(self, timestamp):
        params = {"oldest": timestamp, "inclusive": "false", "limit": 100}
        messages = []
        for _ in range(5):
            body = self.history(**params)
            messages.extend(body.get("messages", []))
            cursor = body.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                if body.get("has_more"):
                    raise RelayError("slack_backlog_skipped", 409)
                return sorted(messages, key=lambda item: decimal.Decimal(item["ts"]))
            params["cursor"] = cursor
        raise RelayError("slack_backlog_skipped", 409)

    def exact(self, timestamp, thread_ts=None):
        params = {"channel": self.config["channel_id"], "oldest": timestamp,
                  "latest": timestamp, "inclusive": "true", "limit": 1}
        if thread_ts:
            body, _ = self.call("conversations.replies", {**params, "ts": thread_ts})
        else:
            body = self.history(**{k: v for k, v in params.items() if k != "channel"})
        messages = [m for m in body.get("messages", []) if m.get("ts") == timestamp]
        if len(messages) != 1:
            raise RelayError("message_not_found", 404)
        return messages[0]


class WeCom:
    def __init__(self, webhook):
        self.webhook, self.http = webhook, opener()

    def send(self, text):
        return self._send({"msgtype": "text", "text": {"content": text}})

    def send_image(self, data, mime):
        validate_image(data, mime)
        return self._send({"msgtype": "image", "image": {
            "base64": base64.b64encode(data).decode("ascii"),
            "md5": hashlib.md5(data).hexdigest(),  # 协议要求的校验和，不用于身份认证。
        }})

    def _send(self, message):
        payload = json.dumps(message, ensure_ascii=False).encode()
        request = urllib.request.Request(self.webhook, data=payload,
                                         headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with self.http.open(request, timeout=20) as response:
                body = json.loads(response.read(8192))
                if response.status == 200 and type(body.get("errcode")) is int and body["errcode"] == 0:
                    return "sent"
                if response.status == 200 and type(body.get("errcode")) is int:
                    return "failed"
        except Exception:
            pass
        # 即使投递成功，也可能发生超时或响应无法读取。
        return "unknown"


def plain_text(text):
    # Slack 链接在企业微信中仍可使用；此处不抓取链接或附件。
    text = re.sub(r"<(https?://[^>|\s]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>\s]+)>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _paragraphs(parts):
    result = ""
    for part in parts:
        if not part:
            continue
        if result:
            trailing = len(result) - len(result.rstrip("\n"))
            leading = len(part) - len(part.lstrip("\n"))
            result += "\n" * max(0, 2 - trailing - leading)
        result += part
    return result


def _elements(node):
    if not isinstance(node, dict) or not isinstance(node.get("elements"), list):
        raise ValueError("unsupported_blocks")
    return node["elements"]


def _inline(nodes):
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("unsupported_blocks")
        kind = node.get("type")
        if kind == "text" and isinstance(node.get("text"), str):
            # 仅含空白的文本节点可能承载所有段落换行。
            result.append(node["text"])
        elif kind == "link" and isinstance(node.get("url"), str):
            url = node["url"]
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in ("http", "https", "mailto")
                    or any(char.isspace() for char in url)):
                raise ValueError("unsupported_blocks")
            label = node.get("text", url)
            if not isinstance(label, str):
                raise ValueError("unsupported_blocks")
            result.append(url if not label or label == url else label + " (" + url + ")")
        elif kind == "emoji" and isinstance(node.get("name"), str):
            name = node["name"]
            if not re.fullmatch(r"[A-Za-z0-9_+:-]{1,100}", name):
                raise ValueError("unsupported_blocks")
            result.append(":" + name + ":")
        else:
            # 不递归收集任意文本、URL 或元数据。
            # 遇到不支持的可见内容时，整条消息使用回退文本。
            raise ValueError("unsupported_blocks")
    return "".join(result)


def _rich_element(node):
    nodes = _elements(node)
    kind = node.get("type")
    if kind in ("rich_text_section", "rich_text_quote", "rich_text_preformatted"):
        text = _inline(nodes)
        if kind == "rich_text_quote":
            return "\n".join("> " + line for line in text.split("\n"))
        if kind == "rich_text_preformatted":
            return "```\n" + text + ("" if text.endswith("\n") else "\n") + "```"
        return text
    if kind == "rich_text_list":
        style, indent, offset = node.get("style"), node.get("indent", 0), node.get("offset", 0)
        if (style not in ("bullet", "ordered") or type(indent) is not int
                or not 0 <= indent <= 8 or type(offset) is not int or not 0 <= offset <= 10000):
            raise ValueError("unsupported_blocks")
        lines = []
        for index, item in enumerate(nodes):
            if not isinstance(item, dict) or item.get("type") != "rich_text_section":
                raise ValueError("unsupported_blocks")
            prefix = "  " * indent + (str(offset + index + 1) + ". " if style == "ordered" else "- ")
            text = _inline(_elements(item))
            lines.append(prefix + text.replace("\n", "\n" + " " * len(prefix)))
        return "\n".join(lines)
    raise ValueError("unsupported_blocks")


def _composition(node):
    if (not isinstance(node, dict) or node.get("type") not in ("plain_text", "mrkdwn")
            or not isinstance(node.get("text"), str)):
        raise ValueError("unsupported_blocks")
    return plain_text(node["text"])


def message_text(message):
    """仅渲染可见文本字段，不遍历附件或附属组件。

    Slack 顶层文本可能将 rich_text 节点中保留的换行压平。
    对支持的结构进行完整解析，否则回退到原始文本；
    不得静默转发仅渲染了一部分的晨报。
    """
    fallback = message.get("text", "")
    fallback = plain_text(fallback) if isinstance(fallback, str) else ""
    blocks = message.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return fallback
    try:
        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("unsupported_blocks")
            kind = block.get("type")
            if kind == "rich_text":
                parts.append(_paragraphs(_rich_element(node) for node in _elements(block)))
            elif kind in ("header", "section"):
                texts = [_composition(block["text"])] if "text" in block else []
                fields = block.get("fields", [])
                if not isinstance(fields, list):
                    raise ValueError("unsupported_blocks")
                texts.extend(_composition(field) for field in fields)
                parts.append(_paragraphs(texts))
            elif kind == "divider":
                parts.append("---")
            else:
                raise ValueError("unsupported_blocks")
        text = _paragraphs(parts)
        return text if text.strip() else fallback
    except ValueError:
        return fallback


def chunks(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_BODY:
        raise RelayError("invalid_text")
    pieces, current, size = [], [], 0
    for char in text:
        length = len(char.encode())
        if size + length > 1900:
            pieces.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += length
    pieces.append("".join(current))
    if len(pieces) > 40:
        raise RelayError("message_too_long")
    return pieces if len(pieces) == 1 else [f"[{i + 1}/{len(pieces)}] {p}" for i, p in enumerate(pieces)]


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        secure_dir(directory)
        path = directory / "state.sqlite3"
        if path.exists() or path.is_symlink():
            os.close(secure_open(path))
        previous = os.umask(0o077)
        try:
            self.db = sqlite3.connect(str(path))
        finally:
            os.umask(previous)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, intent_hash TEXT NOT NULL, source_key TEXT,
          mode TEXT NOT NULL, actor_uid INTEGER, created REAL NOT NULL, message_ts TEXT,
          status TEXT NOT NULL, parts_total INTEGER NOT NULL DEFAULT 0,
          parts_sent INTEGER NOT NULL DEFAULT 0, reason_hash TEXT);
        CREATE TABLE IF NOT EXISTS parts (
          request_id TEXT NOT NULL, part INTEGER NOT NULL, content_hash TEXT NOT NULL,
          status TEXT NOT NULL, PRIMARY KEY(request_id,part));
        CREATE TABLE IF NOT EXISTS image_parts (
          request_id TEXT NOT NULL, file_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
          status TEXT NOT NULL, content_hash TEXT, byte_size INTEGER, error_code TEXT,
          PRIMARY KEY(request_id,file_id));
        """)
        # 采用保守恢复策略：崩溃时正在发送的请求不再重试。
        self.db.execute("UPDATE requests SET status='unknown' WHERE status='sending'")
        self.db.execute("UPDATE parts SET status='unknown' WHERE status='sending'")
        self.db.execute("UPDATE image_parts SET status='unknown' WHERE status='sending'")
        self.db.execute("UPDATE image_parts SET status='not_attempted' WHERE status IN ('pending','downloading')")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.db.commit()

    def status(self, request_id):
        row = self.db.execute("SELECT status,parts_total,parts_sent FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return None
        result = {"request_id": request_id, "status": row[0], "parts_total": row[1], "parts_sent": row[2]}
        images = self.db.execute("SELECT file_id,status,error_code FROM image_parts WHERE request_id=? ORDER BY ordinal", (request_id,)).fetchall()
        if images:
            result["images"] = [{"file_id": item[0], "status": item[1], "error": item[2]} for item in images]
        return result


class Relay:
    def __init__(self, store, slack, wecom, config, clock=time.time, sleeper=time.sleep, notice_reader=None):
        self.store, self.slack, self.wecom, self.config = store, slack, wecom, config
        self.clock, self.sleeper = clock, sleeper
        self.connected = False
        profile = config.get("notice_connector")
        if profile is not None:
            validate_notice_connector(profile)
        self._notice_cache = {}
        self.notice_reader = notice_reader if notice_reader is not None else (notice_format.NoticeManifestReader(profile["report_base_url"]) if profile else None)
        if store is not None:
            saved = store.get("notice_connector_v1")
            expected = digest(profile) if profile is not None else None
            if saved is not None and saved != expected:
                raise RelayError("notice_identity_changed", 409)
            if profile is not None and saved is None:
                store.set("notice_connector_v1", expected)

    def notice(self, message):
        profile = self.config.get("notice_connector")
        if profile is None or message.get("user") != profile["user_id"]:
            return None
        try:
            candidate = _connector_candidate(message, profile, self.config["team_id"], self.config["channel_id"])
            if candidate.get("format") != "article_list_v1":
                return candidate
            key = digest(message)
            if key not in self._notice_cache:
                # 读取失败在游标推进及投递写入之前向外传播。
                manifest = self.notice_reader.read(candidate)
                verified = _bind_notice_manifest(candidate, manifest, profile)
                if len(self._notice_cache) >= 16:
                    self._notice_cache.pop(next(iter(self._notice_cache)))
                self._notice_cache[key] = verified
            return self._notice_cache[key]
        except RelayError:
            return None

    def delivery_text(self, message):
        notice = self.notice(message)
        return notice["text"] if notice else message_text(message)

    def notice_key(self, message):
        notice = self.notice(message)
        return notice["url"] if notice else None

    def baseline(self):
        # 重启或断线恢复后也使用此逻辑，绝不补发停机期间的历史消息。
        timestamp = self.slack.latest()
        if not TS.fullmatch(timestamp):
            raise RelayError("invalid_slack_timestamp", 502)
        self.store.set("cursor", timestamp)
        self.connected = True

    def is_brief(self, message):
        profile = self.config.get("notice_connector")
        if profile is not None and message.get("user") == profile["user_id"]:
            return self.notice(message) is not None
        text = message_text(message)
        subtype = message.get("subtype")
        return (message.get("user") == self.config["sender_id"]
                and subtype in (None, "bot_message", "file_share") and not message.get("edited")
                and message.get("thread_ts", message.get("ts")) == message.get("ts")
                and isinstance(text, str) and bool(TITLE.search(text.lstrip().split("\n", 1)[0])))

    def image_plans(self, message):
        if not self.config.get("forward_images", False) or not self.is_brief(message):
            return []
        notice = self.notice(message)
        return notice["images"] if notice else select_images(message, self.config["team_id"])

    def poll(self):
        if not self.connected:
            self.baseline()
            return
        try:
            messages = self.slack.newer(self.store.get("cursor"))
            for message in messages:
                timestamp = message.get("ts", "")
                if not TS.fullmatch(timestamp):
                    raise RelayError("invalid_slack_timestamp", 502)
                if decimal.Decimal(timestamp) <= decimal.Decimal(self.store.get("cursor")):
                    continue
                # 缓存只跨同一次处理中的重复校验，不跨投递尝试。
                self._notice_cache.clear()
                if self.is_brief(message):
                    self.deliver("auto:" + timestamp, {"mode": "auto", "ts": timestamp},
                                 self.delivery_text(message), timestamp, "auto", os.getuid(),
                                 image_plans=self.image_plans(message), notice_key=self.notice_key(message))
                self.store.set("cursor", timestamp)
        except NoticeReadError:
            # 公开清单访问故障不能触发重连后跳到最新游标。
            raise
        except Exception:
            self.connected = False
            raise

    def dispatch(self, path, payload, uid):
        if uid != os.getuid():
            raise RelayError("forbidden", 403)
        self._notice_cache.clear()
        if not isinstance(payload, dict):
            raise RelayError("invalid_request")
        if path == "/status":
            if set(payload) != {"request_id"}:
                raise RelayError("invalid_request")
            return self.store.status(payload["request_id"]) or {"status": "not_found"}
        if path not in ("/manual/text", "/manual/slack-message"):
            raise RelayError("not_found", 404)
        allowed = {"request_id", "text"} if path.endswith("/text") else {
            "request_id", "message_ts", "thread_ts", "channel_id", "allow_repeat", "reason"}
        if set(payload) - allowed:
            raise RelayError("unexpected_fields")
        request_id = payload.get("request_id", "")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or request_id.startswith("auto:"):
            raise RelayError("invalid_request_id")
        intent = {"path": path, **payload}
        row = self.store.db.execute("SELECT intent_hash FROM requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if row[0] != digest(intent):
                raise RelayError("request_id_conflict", 409)
            return self.store.status(request_id)
        if path.endswith("/text"):
            return self.deliver(request_id, intent, payload.get("text"), None, "manual_text", uid)
        if payload.get("channel_id", self.config["channel_id"]) != self.config["channel_id"]:
            raise RelayError("channel_forbidden", 403)
        timestamp, thread_ts = payload.get("message_ts", ""), payload.get("thread_ts")
        if not isinstance(timestamp, str) or not TS.fullmatch(timestamp):
            raise RelayError("invalid_message_ts")
        if thread_ts is not None and (not isinstance(thread_ts, str) or not TS.fullmatch(thread_ts)):
            raise RelayError("invalid_thread_ts")
        repeat = payload.get("allow_repeat", False)
        reason = payload.get("reason", "")
        if not isinstance(repeat, bool) or (repeat and (not isinstance(reason, str) or not reason.strip())):
            raise RelayError("repeat_requires_reason")
        message = self.slack.exact(timestamp, thread_ts)
        if message.get("ts") != timestamp:
            raise RelayError("message_mismatch", 502)
        return self.deliver(request_id, intent, self.delivery_text(message),
                            timestamp, "manual_slack", uid, repeat, reason,
                            image_plans=self.image_plans(message), notice_key=self.notice_key(message))

    def deliver(self, request_id, intent, text, timestamp, mode, uid, repeat=False, reason="", image_plans=(), notice_key=None):
        pieces = chunks(text)
        source = digest([self.config["team_id"], self.config["channel_id"], timestamp]) if timestamp else None
        edition = None
        if notice_key is not None:
            notice_url_day(notice_key, validate_notice_connector(self.config.get("notice_connector")))
            edition = "notice_edition:" + digest([self.config["team_id"], self.config["channel_id"], notice_key])
        db = self.store.db
        verified_images = {}
        # 预检仍属纯读取，但必须与来源准入共享事务锁，避免竞争消费者同时发出。
        # 超时或进程退出回滚整笔事务，不消费请求、版次或游标；提交后才可外部写入。
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT intent_hash FROM requests WHERE id=?", (request_id,)).fetchone()
            if row:
                if row[0] != digest(intent):
                    raise RelayError("request_id_conflict", 409)
                return self.store.status(request_id)
            edition_request = self.store.get(edition) if edition else None
            prior = db.execute("SELECT status FROM requests WHERE source_key=? OR message_ts=? OR id=? ORDER BY created DESC LIMIT 1",
                               (source, timestamp, edition_request)).fetchone() if source else None
            status, preflight_error = "sending", None
            if prior and not repeat:
                status = "already_sent" if prior[0] in ("sent", "already_sent") else "source_already_attempted"
            if status == "sending":
                verified_images, preflight_error = self.prepare_notice_images(image_plans)
                if preflight_error:
                    status = "failed"
            db.execute("INSERT INTO requests(id,intent_hash,source_key,mode,actor_uid,created,message_ts,status,parts_total,reason_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (request_id, digest(intent), source, mode, uid, self.clock(), timestamp, status, len(pieces) + len(image_plans), digest(reason) if reason else None))
            if edition and (edition_request is None or status in ("sending", "failed")):
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (edition, request_id))
            if status in ("sending", "failed"):
                for index, plan in enumerate(image_plans):
                    image_status, error_code = "pending", None
                    if preflight_error:
                        image_status = "failed" if plan["file_id"] == preflight_error[0] else "not_attempted"
                        error_code = preflight_error[1] if image_status == "failed" else None
                    db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status,error_code) VALUES (?,?,?,?,?)",
                               (request_id, plan["file_id"], index, image_status, error_code))
        if status != "sending":
            return self.store.status(request_id)
        for index, piece in enumerate(pieces):
            self.wait_slot()
            db.execute("INSERT INTO parts VALUES (?,?,?,'sending')", (request_id, index, digest(piece)))
            db.commit()
            self.store.set("next_send", self.clock() + 3.2)
            try:
                outcome = self.wecom.send(piece)
            except Exception:
                outcome = "unknown"
            if outcome not in ("sent", "failed", "unknown"):
                outcome = "unknown"
            db.execute("UPDATE parts SET status=? WHERE request_id=? AND part=?", (outcome, request_id, index))
            if outcome == "sent":
                db.execute("UPDATE requests SET parts_sent=parts_sent+1 WHERE id=?", (request_id,))
            else:
                db.execute("UPDATE requests SET status=? WHERE id=?", (outcome, request_id))
            db.commit()
            if outcome != "sent":
                db.execute("UPDATE image_parts SET status='not_attempted' WHERE request_id=? AND status='pending'", (request_id,))
                db.commit()
                return self.store.status(request_id)
        return self.deliver_images(request_id, image_plans, verified_images)

    def wait_slot(self):
        while self.clock() < float(self.store.get("next_send", "0")):
            self.sleeper(min(5, float(self.store.get("next_send")) - self.clock()))

    def prepare_notice_images(self, plans):
        # 仅下载与验证，不改账本；调用方在同一准入事务中持锁，随后发送同一份字节。
        verified = {}
        for plan in plans:
            if "expected_sha256" not in plan:
                continue
            file_id = plan["file_id"]
            try:
                if "error" in plan:
                    raise ImageError(plan["error"])
                data = self.slack.download_image(plan)
                validate_image(data, plan["mime"])
                if hashlib.sha256(data).hexdigest() != plan["expected_sha256"]:
                    raise ImageError("notice_poster_hash_mismatch")
                verified[file_id] = data
            except ImageError as error:
                if error.code in {"image_download_failed", "image_download_timed_out", "image_download_incomplete"}:
                    raise NoticeReadError("notice_poster_read_retry") from None
                return {}, (file_id, error.code)
            except Exception:
                raise NoticeReadError("notice_poster_read_retry") from None
        return verified, None

    def deliver_images(self, request_id, plans, verified_images=None):
        db, failed = self.store.db, False
        for plan in plans:
            file_id = plan["file_id"]
            try:
                if "error" in plan:
                    raise ImageError(plan["error"])
                db.execute("UPDATE image_parts SET status='downloading' WHERE request_id=? AND file_id=?", (request_id, file_id))
                db.commit()
                if "expected_sha256" in plan:
                    if verified_images is None or file_id not in verified_images:
                        raise ImageError("notice_poster_preflight_required")
                    data = verified_images[file_id]
                else:
                    data = self.slack.download_image(plan)
                validate_image(data, plan["mime"])
                if "expected_sha256" in plan and hashlib.sha256(data).hexdigest() != plan["expected_sha256"]:
                    raise ImageError("notice_poster_hash_mismatch")
            except Exception as error:
                code = error.code if isinstance(error, ImageError) else "image_download_failed"
                db.execute("UPDATE image_parts SET status='failed',error_code=? WHERE request_id=? AND file_id=?", (code, request_id, file_id))
                db.commit()
                failed = True
                continue
            self.wait_slot()
            db.execute("UPDATE image_parts SET status='sending',content_hash=?,byte_size=? WHERE request_id=? AND file_id=?", (hashlib.sha256(data).hexdigest(), len(data), request_id, file_id))
            db.commit()
            self.store.set("next_send", self.clock() + 3.2)
            try:
                outcome = self.wecom.send_image(data, plan["mime"])
            except Exception:
                outcome = "unknown"
            finally:
                del data
            if outcome not in ("sent", "failed", "unknown"):
                outcome = "unknown"
            db.execute("UPDATE image_parts SET status=? WHERE request_id=? AND file_id=?", (outcome, request_id, file_id))
            if outcome == "sent":
                db.execute("UPDATE requests SET parts_sent=parts_sent+1 WHERE id=?", (request_id,))
            elif outcome == "unknown":
                db.execute("UPDATE requests SET status='unknown' WHERE id=?", (request_id,))
                db.execute("UPDATE image_parts SET status='not_attempted' WHERE request_id=? AND status='pending'", (request_id,))
                db.commit()
                return self.store.status(request_id)
            else:
                failed = True
            db.commit()
        db.execute("UPDATE requests SET status=? WHERE id=?", ("partial" if failed else "sent", request_id))
        db.commit()
        return self.store.status(request_id)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "PrivateRelay"
    # 基类在 setup 阶段设置套接字超时，覆盖请求行和请求头解析之前的等待。
    timeout = 10

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.connection.settimeout(10)
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise RelayError("forbidden", 403)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise RelayError("invalid_encoding")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise RelayError("invalid_length", 413)
            payload = json.loads(self.rfile.read(length))
            result = self.server.relay.dispatch(self.path, payload, uid)
            self.reply(200, result)
        except RelayError as error:
            self.reply(error.status, {"error": error.code})
        except Exception:
            self.reply(400, {"error": "request_failed"})

    def reply(self, status, result):
        data = json.dumps(result).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ValueError):
            pass


@contextlib.contextmanager
def exclusive(root):
    import fcntl
    lock_path = root / "run" / "service.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(lock_fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(lock_fd)
        raise RelayError("unsafe_lock", 500)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise RelayError("already_running", 409) from None
    try:
        yield
    finally:
        os.close(lock_fd)


def serve(root, relay):
    socket_path = root / "run" / "relay.sock"
    if socket_path.exists() or socket_path.is_symlink():
        info = socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RelayError("unsafe_socket", 500)
        socket_path.unlink()
    relay.baseline()
    previous = os.umask(0o077)
    try:
        server = socketserver.UnixStreamServer(str(socket_path), Handler)
        os.chmod(str(socket_path), 0o600)
    finally:
        os.umask(previous)
    server.relay, server.timeout = relay, 1
    next_poll = time.monotonic() + relay.config["poll_seconds"]
    try:
        while True:
            server.handle_request()
            if time.monotonic() >= next_poll:
                try:
                    relay.poll()
                except NoticeReadError:
                    # 尚未发送，保留游标后重试公开清单读取。
                    print('{"event":"notice_read_retry"}', flush=True)
                except Exception:
                    # 不记录消息正文、凭据、URL 或原始异常。
                    print('{"event":"poll_paused_until_new_baseline"}', flush=True)
                next_poll = time.monotonic() + relay.config["poll_seconds"]
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=600)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "preflight", "manual", "status"))
    parser.add_argument("--root", type=Path, default=Path("/relay"))
    parser.add_argument("--kind", choices=("text", "slack-message"))
    args = parser.parse_args()
    if args.command in ("manual", "status"):
        secure_dir(args.root)
        secure_dir(args.root / "run")
        connection = UnixConnection(args.root / "run" / "relay.sock")
        payload = sys.stdin.buffer.read(MAX_BODY + 1)
        if len(payload) > MAX_BODY:
            raise RelayError("request_too_large", 413)
        path = "/status" if args.command == "status" else "/manual/" + (args.kind or "text")
        connection.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read(8192))
        print(json.dumps(data))
        connection.close()
        return
    config = load_config(args.root)
    with exclusive(args.root):
        secrets = load_secrets(args.root)
        slack = Slack(secrets["slack_user_token"], config)
        slack.verify()
        relay = Relay(Store(args.root / "state"), slack, WeCom(secrets["wecom_webhook"]), config)
        if args.command == "preflight":
            # 仅执行只读网络调用，建立基线，不发送消息。
            relay.baseline()
            print('{"identity_verified":true,"scopes_verified":true,"baseline_saved":true,"messages_sent":0}')
        else:
            serve(args.root, relay)


if __name__ == "__main__":
    try:
        main()
    except RelayError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception:
        print('{"error":"operation_failed"}', file=sys.stderr)
        sys.exit(1)
