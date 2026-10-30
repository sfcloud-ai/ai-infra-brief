"""轮询固定私有频道，持久化请求与回执，不连接企微或部署入口。"""

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request

if __package__ == "ai_brief_control":
    from ai_brief_relay import relay as security
else:
    from integrations.slack_wecom import relay as security


COMMANDS = ("help", "ping", "status")
MAX_AGE = 300
MAX_REPLY = 1024 * 1024
REQUEST = re.compile(r"!ai-brief ([A-Za-z0-9][A-Za-z0-9_-]{0,63}) ([a-z]+)")
TIMESTAMP = re.compile(r"[0-9]{10,12}\.[0-9]{6}")
SCOPES = {"groups:read", "groups:history", "chat:write"}


class ControlError(Exception):
    pass


class RateLimited(ControlError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__("slack_rate_limited")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def valid_id(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Z0-9]+", value)


def validate_config(config):
    required = {"team_id", "channel_id", "responder_user_id", "responder_bot_id",
                "allowed_users", "allowed_bots", "poll_seconds"}
    if not isinstance(config, dict) or set(config) != required:
        raise ControlError("invalid_config")
    for key, prefix in (("team_id", "T"), ("channel_id", "[CG]"),
                        ("responder_user_id", "[UW]"), ("responder_bot_id", "B")):
        if not valid_id(config[key], prefix):
            raise ControlError("invalid_config")
    users, bots = config["allowed_users"], config["allowed_bots"]
    if (not isinstance(users, list) or not isinstance(bots, list)
            or not 1 <= len(users) + len(bots) <= 20
            or any(not valid_id(user, "[UW]") for user in users)
            or len(set(users)) != len(users)
            or config["responder_user_id"] in users):
        raise ControlError("invalid_callers")
    for bot in bots:
        if (not isinstance(bot, dict) or set(bot) != {"app_id", "bot_id"}
                or not valid_id(bot["app_id"], "A") or not valid_id(bot["bot_id"], "B")
                or bot["bot_id"] == config["responder_bot_id"]):
            raise ControlError("invalid_callers")
    if len({encoded(bot) for bot in bots}) != len(bots):
        raise ControlError("invalid_callers")
    if type(config["poll_seconds"]) is not int or not 60 <= config["poll_seconds"] <= 120:
        raise ControlError("invalid_poll_seconds")
    return config


def load(root):
    if not root.is_absolute() or root.resolve() != root:
        raise ControlError("absolute_unlinked_root_required")
    for path in (root, root / "state", root / "run", root / "secrets"):
        security.secure_dir(path)
    config = validate_config(json.loads(security.secure_read(root / "config.json")))
    credentials = json.loads(security.secure_read(root / "secrets/bot.json"))
    if (not isinstance(credentials, dict) or set(credentials) != {"slack_bot_token"}
            or not isinstance(credentials["slack_bot_token"], str)
            or not re.fullmatch(r"xoxb-[A-Za-z0-9-]+", credentials["slack_bot_token"])):
        raise ControlError("bot_token_required")
    return config, credentials["slack_bot_token"]


@contextlib.contextmanager
def deadline(seconds):
    """Linux 单线程硬超时，覆盖 DNS、TLS、响应读取和固定处理函数。"""
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise ControlError("timer_in_use")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise TimeoutError("deadline_exceeded")

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Slack:
    def __init__(self, token, config):
        self.token, self.config = token, config
        self.http = security.opener()

    def call(self, method, params):
        if method not in {"auth.test", "conversations.info", "conversations.history", "chat.postMessage"}:
            raise ControlError("method_forbidden")
        request = urllib.request.Request("https://slack.com/api/" + method,
                                         data=encoded(params).encode(),
                                         headers={"Authorization": "Bearer " + self.token,
                                                  "Content-Type": "application/json"})
        try:
            with deadline(15), self.http.open(request, timeout=10) as response:
                raw = response.read(MAX_REPLY + 1)
                if len(raw) > MAX_REPLY:
                    raise ControlError("slack_response_too_large")
                body = json.loads(raw)
                scopes = set(response.headers.get("x-oauth-scopes", "").replace(" ", "").split(","))
        except urllib.error.HTTPError as error:
            if error.code == 429:
                try:
                    seconds = max(60, min(3600, int(error.headers.get("Retry-After", "60"))))
                except (TypeError, ValueError):
                    seconds = 60
                raise RateLimited(seconds) from None
            raise ControlError("slack_transport_unknown") from None
        except Exception:
            raise ControlError("slack_transport_unknown") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise ControlError("slack_api_rejected")
        return body, scopes

    def verify(self):
        body, scopes = self.call("auth.test", {})
        if (body.get("team_id") != self.config["team_id"]
                or body.get("user_id") != self.config["responder_user_id"]
                or body.get("bot_id") != self.config["responder_bot_id"]
                or not SCOPES <= scopes):
            raise ControlError("wrong_identity_or_scopes")
        self.verify_channel()

    def verify_channel(self):
        body, _ = self.call("conversations.info", {"channel": self.config["channel_id"]})
        channel = body.get("channel", {})
        if (channel.get("id") != self.config["channel_id"] or channel.get("is_private") is not True
                or channel.get("is_member") is not True or channel.get("is_archived") is not False
                or channel.get("is_im") is not False or channel.get("is_mpim") is not False
                or channel.get("is_shared") is not False or channel.get("is_ext_shared") is not False
                or channel.get("is_org_shared") is not False or channel.get("pending_shared")
                or channel.get("is_pending_ext_shared")):
            raise ControlError("private_local_member_channel_required")

    def history(self, oldest, latest):
        # 固定快照取全后再执行，分页失败不会推进游标或只处理半页。
        messages, cursor, seen = {}, "", set()
        for _ in range(10):
            params = {"channel": self.config["channel_id"], "oldest": oldest,
                      "latest": latest, "inclusive": False, "limit": 15}
            if cursor:
                params["cursor"] = cursor
            body, _ = self.call("conversations.history", params)
            batch = body.get("messages")
            if not isinstance(batch, list):
                raise ControlError("invalid_history")
            for message in batch:
                ts = message.get("ts") if isinstance(message, dict) else None
                if not isinstance(ts, str) or not TIMESTAMP.fullmatch(ts):
                    raise ControlError("invalid_history")
                if float(oldest) < float(ts) < float(latest):
                    if ts in messages and messages[ts] != message:
                        raise ControlError("conflicting_history")
                    messages[ts] = message
            cursor = body.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                if body.get("has_more"):
                    raise ControlError("incomplete_history")
                return sorted(messages.values(), key=lambda message: float(message["ts"]))
            if not isinstance(cursor, str) or cursor in seen:
                raise ControlError("invalid_history_cursor")
            seen.add(cursor)
        raise ControlError("history_limit_exceeded")

    def reply(self, receipt):
        body, _ = self.call("chat.postMessage", {
            "channel": self.config["channel_id"], "thread_ts": receipt["source_ts"],
            "text": encoded(receipt), "mrkdwn": False, "parse": "none",
            "unfurl_links": False, "unfurl_media": False,
        })
        ts = body.get("ts")
        if (body.get("channel") != self.config["channel_id"]
                or not isinstance(ts, str) or not TIMESTAMP.fullmatch(ts)):
            raise ControlError("invalid_send_receipt")
        return ts


class Store:
    def __init__(self, root, config):
        path = root / "state/control.sqlite3"
        if path.exists() or path.is_symlink():
            os.close(security.secure_open(path))
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests (
            source_ts TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
            intent_hash TEXT NOT NULL, expires REAL NOT NULL,
            receipt TEXT NOT NULL, delivery TEXT NOT NULL, reply_ts TEXT);
        """)
        binding = fingerprint({key: value for key, value in config.items() if key != "poll_seconds"})
        if self.get("binding", binding) != binding:
            self.db.close()
            raise ControlError("ledger_binding_changed")
        self.set("binding", binding)
        # 可能已到 Slack 的回执不重发，未完成计算也不重新执行。
        self.db.execute("UPDATE requests SET delivery='unknown' WHERE delivery='sending'")
        rows = self.db.execute("SELECT * FROM requests WHERE delivery='executing'").fetchall()
        for row in rows:
            receipt = json.loads(row["receipt"])
            receipt.update(status="interrupted", code="restart_interrupted", result={})
            self.db.execute("UPDATE requests SET receipt=?, delivery='prepared' WHERE source_ts=?",
                            (encoded(receipt), row["source_ts"]))
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
        self.db.commit()


class Controller:
    def __init__(self, store, slack, config, clock=time.time, monotonic=time.monotonic, sleeper=time.sleep):
        self.store, self.slack, self.config = store, slack, config
        self.clock, self.monotonic, self.sleeper = clock, monotonic, sleeper
        self.started = monotonic()

    def actor(self, message):
        if (message.get("type") != "message" or message.get("subtype") not in (None, "bot_message")
                or message.get("edited") or message.get("hidden")
                or message.get("thread_ts", message.get("ts")) != message.get("ts")
                or message.get("channel", self.config["channel_id"]) != self.config["channel_id"]
                or message.get("team", self.config["team_id"]) != self.config["team_id"]
                or message.get("user") == self.config["responder_user_id"]
                or message.get("bot_id") == self.config["responder_bot_id"]):
            return None
        if any(key in message for key in ("bot_id", "app_id", "bot_profile")) or message.get("subtype") == "bot_message":
            identity = {"app_id": message.get("app_id"), "bot_id": message.get("bot_id")}
            return "bot:" + encoded(identity) if identity in self.config["allowed_bots"] else None
        user = message.get("user")
        return "user:" + user if user in self.config["allowed_users"] else None

    def execute(self, command):
        if command == "help":
            return {"commands": list(COMMANDS), "syntax": "!ai-brief <request_id> <command>", "max_age_seconds": MAX_AGE}
        if command == "ping":
            return {"pong": True}
        if command == "status":
            return {"service": "slack_control", "state": "running", "read_only": True,
                    "uptime_seconds": max(0, int(self.monotonic() - self.started)),
                    "relay_health": "not_checked", "brief_pipeline": "not_connected"}
        raise ControlError("unknown_command")

    def accept(self, message):
        actor = self.actor(message)
        text, ts = message.get("text"), message.get("ts")
        if (actor is None or not isinstance(text, str) or not text.startswith("!ai-brief")
                or not isinstance(ts, str) or not TIMESTAMP.fullmatch(ts)
                or not 0 <= self.clock() - float(ts) <= MAX_AGE):
            return
        db = self.store.db
        if db.execute("SELECT 1 FROM requests WHERE source_ts=?", (ts,)).fetchone():
            return
        match = REQUEST.fullmatch(text) if len(text) <= 160 else None
        request_id, command = match.groups() if match else ("invalid:" + fingerprint(ts)[:32], None)
        intent = fingerprint([actor, text])
        prior = db.execute("SELECT intent_hash FROM requests WHERE request_id=?", (request_id,)).fetchone()
        code = "invalid_request" if not match else "unknown_command" if command not in COMMANDS else "ok"
        if prior:
            if prior[0] == intent:
                return
            # 冲突回执使用源事件 ID，不覆盖或暴露旧调用者的结果。
            request_id, command, code = "conflict:" + fingerprint(ts)[:32], None, "request_id_conflict"
        receipt = {"type": "ai_brief_control_receipt", "v": 1, "request_id": request_id,
                   "source_ts": ts, "command": command if command in COMMANDS else None,
                   "status": "ok" if code == "ok" else "rejected", "code": code, "result": {}}
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?,'executing',NULL)",
                   (ts, request_id, intent, float(ts) + MAX_AGE, encoded(receipt)))
        db.commit()
        try:
            if code == "ok":
                with deadline(2):
                    receipt["result"] = self.execute(command)
        except TimeoutError:
            receipt.update(status="timeout", code="command_timed_out", result={})
        except Exception:
            receipt.update(status="failed", code="command_failed", result={})
        db.execute("UPDATE requests SET receipt=?, delivery='prepared' WHERE source_ts=?", (encoded(receipt), ts))
        db.commit()

    def flush(self):
        db = self.store.db
        for row in db.execute("SELECT * FROM requests WHERE delivery='prepared' ORDER BY source_ts").fetchall():
            # 避免频道速率突发，并在重启后保留下一发送时间。
            delay = float(self.store.get("next_send", "0")) - self.clock()
            if delay > 0:
                self.sleeper(min(delay, 60))
                if self.clock() < float(self.store.get("next_send", "0")):
                    return
            if self.clock() > row["expires"]:
                db.execute("UPDATE requests SET delivery='expired' WHERE source_ts=?", (row["source_ts"],))
                db.commit()
                continue
            db.execute("UPDATE requests SET delivery='sending' WHERE source_ts=?", (row["source_ts"],))
            db.commit()
            self.store.set("next_send", self.clock() + 1.1)
            try:
                reply_ts = self.slack.reply(json.loads(row["receipt"]))
            except RateLimited as error:
                # 429 是明确拒绝，本请求不重试，其余回执等待服务器要求的时间。
                db.execute("UPDATE requests SET delivery='failed' WHERE source_ts=?", (row["source_ts"],))
                db.commit()
                self.store.set("next_send", self.clock() + error.seconds)
                raise
            except Exception:
                db.execute("UPDATE requests SET delivery='unknown' WHERE source_ts=?", (row["source_ts"],))
            else:
                db.execute("UPDATE requests SET delivery='sent', reply_ts=? WHERE source_ts=?", (reply_ts, row["source_ts"]))
            db.commit()

    def poll(self):
        self.slack.verify_channel()
        now = self.clock()
        if self.store.get("cursor") is None:
            # 首次启用不执行历史，重启则保留游标并只接受五分钟内的新请求。
            self.store.set("cursor", f"{now:.6f}")
        oldest = max(float(self.store.get("cursor")), now - MAX_AGE)
        latest = f"{now:.6f}"
        if oldest < now:
            messages = self.slack.history(f"{oldest:.6f}", latest)
            for message in messages:
                self.accept(message)
                self.store.set("cursor", message["ts"])
        self.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capabilities", "preflight", "serve"))
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.command == "capabilities":
        print(encoded({"commands": list(COMMANDS), "read_only": True, "messages_sent": 0}))
        return
    if args.root is None:
        raise ControlError("root_required")
    os.umask(0o077)
    config, token = load(args.root)
    slack = Slack(token, config)
    with security.exclusive(args.root):
        slack.verify()
        if args.command == "preflight":
            # 不创建账本、不推进游标、不发送测试消息。
            print(encoded({"identity_verified": True, "channel_verified": True, "messages_sent": 0}))
            return
        store = Store(args.root, config)
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        previous = signal.signal(signal.SIGTERM, stop)
        try:
            controller = Controller(store, slack, config)
            while not stopping:
                wait = config["poll_seconds"]
                try:
                    controller.poll()
                except RateLimited as error:
                    wait = max(wait, error.seconds)
                    print('{"event":"control_rate_limited"}', flush=True)
                except Exception:
                    print('{"event":"control_poll_failed"}', flush=True)
                until = time.monotonic() + wait
                while not stopping and time.monotonic() < until:
                    time.sleep(min(1, until - time.monotonic()))
        finally:
            signal.signal(signal.SIGTERM, previous)
            store.db.close()


def entrypoint():
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        # 不回显异常、配置、请求正文或凭据。
        print('{"error":"control_service_failed"}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    entrypoint()
