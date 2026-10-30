"""私有的单工作区 Slack → 企业微信转发器，仅使用标准库。"""
from __future__ import annotations

import argparse
import base64
import contextlib
import decimal
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

from images import ImageError, MAX_IMAGE_BYTES, image_url, select_images, validate_image

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
    if (not required <= set(config) or set(config) - required - {"forward_images"}
            or not 30 <= config["poll_seconds"] <= 3600
            or type(config.get("forward_images", False)) is not bool):
        raise RelayError("invalid_config", 500)
    for key, prefix in (("team_id", "T"), ("user_id", "U"),
                        ("channel_id", "D"), ("sender_id", "U")):
        if not re.fullmatch(prefix + r"[A-Z0-9]+", config[key]):
            raise RelayError("invalid_config", 500)
    return config


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
    def __init__(self, store, slack, wecom, config, clock=time.time, sleeper=time.sleep):
        self.store, self.slack, self.wecom, self.config = store, slack, wecom, config
        self.clock, self.sleeper = clock, sleeper
        self.connected = False

    def baseline(self):
        # 重启或断线恢复后也使用此逻辑，绝不补发停机期间的历史消息。
        timestamp = self.slack.latest()
        if not TS.fullmatch(timestamp):
            raise RelayError("invalid_slack_timestamp", 502)
        self.store.set("cursor", timestamp)
        self.connected = True

    def is_brief(self, message):
        text = message_text(message)
        subtype = message.get("subtype")
        return (message.get("user") == self.config["sender_id"]
                and subtype in (None, "bot_message", "file_share") and not message.get("edited")
                and message.get("thread_ts", message.get("ts")) == message.get("ts")
                and isinstance(text, str) and bool(TITLE.search(text.lstrip().split("\n", 1)[0])))

    def image_plans(self, message):
        if not self.config.get("forward_images", False) or not self.is_brief(message):
            return []
        return select_images(message, self.config["team_id"])

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
                if self.is_brief(message):
                    self.deliver("auto:" + timestamp, {"mode": "auto", "ts": timestamp},
                                 message_text(message), timestamp, "auto", os.getuid(),
                                 image_plans=self.image_plans(message))
                self.store.set("cursor", timestamp)
        except Exception:
            self.connected = False
            raise

    def dispatch(self, path, payload, uid):
        if uid != os.getuid():
            raise RelayError("forbidden", 403)
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
        return self.deliver(request_id, intent, message_text(message),
                            timestamp, "manual_slack", uid, repeat, reason,
                            image_plans=self.image_plans(message))

    def deliver(self, request_id, intent, text, timestamp, mode, uid, repeat=False, reason="", image_plans=()):
        pieces = chunks(text)
        source = digest([self.config["team_id"], self.config["channel_id"], timestamp]) if timestamp else None
        db = self.store.db
        row = db.execute("SELECT intent_hash FROM requests WHERE id=?", (request_id,)).fetchone()
        if row:
            if row[0] != digest(intent):
                raise RelayError("request_id_conflict", 409)
            return self.store.status(request_id)
        prior = db.execute("SELECT status FROM requests WHERE source_key=? ORDER BY created DESC LIMIT 1", (source,)).fetchone() if source else None
        status = "sending"
        if prior and not repeat:
            status = "already_sent" if prior[0] in ("sent", "already_sent") else "source_already_attempted"
        db.execute("INSERT INTO requests(id,intent_hash,source_key,mode,actor_uid,created,message_ts,status,parts_total,reason_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (request_id, digest(intent), source, mode, uid, self.clock(), timestamp, status, len(pieces) + len(image_plans), digest(reason) if reason else None))
        if status == "sending":
            for index, plan in enumerate(image_plans):
                db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status) VALUES (?,?,?,'pending')", (request_id, plan["file_id"], index))
        db.commit()
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
        return self.deliver_images(request_id, image_plans)

    def wait_slot(self):
        while self.clock() < float(self.store.get("next_send", "0")):
            self.sleeper(min(5, float(self.store.get("next_send")) - self.clock()))

    def deliver_images(self, request_id, plans):
        db, failed = self.store.db, False
        for plan in plans:
            file_id = plan["file_id"]
            try:
                if "error" in plan:
                    raise ImageError(plan["error"])
                db.execute("UPDATE image_parts SET status='downloading' WHERE request_id=? AND file_id=?", (request_id, file_id))
                db.commit()
                data = self.slack.download_image(plan)
                validate_image(data, plan["mime"])
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
