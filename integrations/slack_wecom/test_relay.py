"""离线契约测试，仅使用模拟传输层；运行时使用 --network none。"""
import contextlib
import io
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

import relay as r

CONFIG = {"team_id": "TTEST", "user_id": "UOWNER", "channel_id": "DFIXED",
          "sender_id": "UDOT", "poll_seconds": 60}


def ts(n):
    return f"1700000000.{n:06d}"


def brief(n, **changes):
    return {"ts": ts(n), "user": "UDOT", "text": "*10 月 29 日 AI Infra 晨报*\nTest body", **changes}


def rich_section(*nodes):
    return {"type": "rich_text_section", "elements": list(nodes)}


def text_node(text):
    return {"type": "text", "text": text}


def redacted_observed_message(n=1):
    # 脱敏的富文本结构：rich_text → section → 三个文本节点。
    # 中间节点仅含两个 LF 字符；已移除原始正文和标识符。
    title, body = "AI Infra 晨报", "脱敏测试正文，不包含用户原始文本。"
    return brief(n, text=title + "  " + body, blocks=[{
        "type": "rich_text", "elements": [rich_section(
            text_node(title), text_node("\n\n"), text_node(body))]}])


class FakeSlack:
    def __init__(self):
        self.messages = []
        self.calls = []
        self.fail = False

    def latest(self):
        self.calls.append("latest")
        return self.messages[-1]["ts"] if self.messages else ts(0)

    def newer(self, timestamp):
        self.calls.append("newer")
        if self.fail:
            raise r.RelayError("offline")
        return [m for m in self.messages if m["ts"] > timestamp]

    def exact(self, timestamp, thread_ts=None):
        self.calls.append("exact")
        for message in self.messages:
            if message["ts"] == timestamp:
                return message
        raise r.RelayError("message_not_found", 404)


class FakeWeCom:
    def __init__(self):
        self.sent = []
        self.results = []

    def send(self, text):
        self.sent.append(text)
        result = self.results.pop(0) if self.results else "sent"
        if isinstance(result, Exception):
            raise result
        return result


class Case(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        for name in ("secrets", "state", "run"):
            (self.root / name).mkdir(mode=0o700)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.config_path.chmod(0o600)
        self.store = r.Store(self.root / "state")
        self.slack, self.wecom = FakeSlack(), FakeWeCom()
        self.now = 1000.0
        self.relay = r.Relay(self.store, self.slack, self.wecom, CONFIG,
                             clock=lambda: self.now, sleeper=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def call(self, kind="text", **payload):
        return self.relay.dispatch("/manual/" + kind, payload, os.getuid())

    def test_initial_baseline_never_sends_history(self):
        self.slack.messages = [brief(1), brief(2)]
        self.relay.poll()
        self.assertEqual(self.store.get("cursor"), ts(2))
        self.assertEqual(self.wecom.sent, [])
        self.slack.messages.append(brief(3))
        self.relay.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.relay.poll()
        self.assertEqual(len(self.wecom.sent), 1)

    def test_only_known_sender_title_and_new_root_messages(self):
        self.relay.baseline()
        self.slack.messages = [brief(1, user="UOTHER"), brief(2, text="Ordinary chat"),
                               brief(3, edited={"ts": ts(9)}), brief(4, thread_ts=ts(1)),
                               brief(5, subtype="message_changed"), brief(6)]
        self.relay.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.store.get("cursor"), ts(6))

    def test_reconnect_skips_downtime_history(self):
        self.relay.baseline()
        self.slack.fail = True
        with self.assertRaises(r.RelayError):
            self.relay.poll()
        self.slack.messages.append(brief(1))
        self.slack.fail = False
        self.relay.poll()
        self.assertEqual(self.store.get("cursor"), ts(1))
        self.assertEqual(self.wecom.sent, [])

    def test_restart_takes_new_baseline(self):
        self.relay.baseline()
        self.slack.messages.append(brief(1))
        restarted = r.Relay(self.store, self.slack, self.wecom, CONFIG)
        restarted.poll()
        self.assertEqual(self.wecom.sent, [])
        self.assertEqual(self.store.get("cursor"), ts(1))

    def test_request_id_dedup_and_conflict(self):
        one = self.call(request_id="one", text="hello")
        self.assertEqual(self.call(request_id="one", text="hello"), one)
        with self.assertRaises(r.RelayError) as caught:
            self.call(request_id="one", text="changed")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.wecom.sent, ["hello"])

    def test_rich_auto_manual_and_source_dedup(self):
        self.relay.baseline()
        message = redacted_observed_message()
        self.slack.messages = [message]
        self.relay.poll()
        expected = "AI Infra 晨报\n\n脱敏测试正文，不包含用户原始文本。"
        self.assertEqual(self.wecom.sent, [expected])
        self.relay.poll()
        result = self.call("slack-message", request_id="same-rich-source", message_ts=ts(1))
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(self.wecom.sent, [expected])

    def test_renderer_upgrade_does_not_resend_old_source(self):
        message = redacted_observed_message()
        self.slack.messages = [message]
        self.relay.deliver("auto:" + ts(1), {"mode": "auto", "ts": ts(1)},
                           message["text"], ts(1), "auto", os.getuid())
        self.relay.deliver("auto:" + ts(1), {"mode": "auto", "ts": ts(1)},
                           r.message_text(message), ts(1), "auto", os.getuid())
        result = self.call("slack-message", request_id="after-upgrade", message_ts=ts(1))
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(len(self.wecom.sent), 1)
        self.relay.baseline()
        self.relay.poll()
        self.assertEqual(len(self.wecom.sent), 1)

    def test_manual_rich_message_preserves_breaks(self):
        self.slack.messages = [redacted_observed_message()]
        self.call("slack-message", request_id="rich-manual", message_ts=ts(1))
        self.assertEqual(self.wecom.sent[0].count("\n"), 2)

    def test_wrong_channel_is_rejected_before_read(self):
        with self.assertRaises(r.RelayError) as caught:
            self.call("slack-message", request_id="bad", channel_id="DOTHER", message_ts=ts(1))
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.slack.calls, [])

    def test_exact_old_single_message_leaves_auto_cursor(self):
        self.slack.messages = [brief(1), brief(2)]
        self.relay.baseline()
        self.call("slack-message", request_id="manual-old", message_ts=ts(1))
        self.assertEqual(self.wecom.sent, [brief(1)["text"]])
        self.assertEqual(self.store.get("cursor"), ts(2))

    def test_wrong_exact_response_does_not_forward(self):
        with patch.object(self.slack, "exact", return_value=brief(2)):
            with self.assertRaises(r.RelayError):
                self.call("slack-message", request_id="wrong", message_ts=ts(1))
        self.assertEqual(self.wecom.sent, [])

    def test_auto_and_manual_source_dedup(self):
        self.relay.baseline()
        self.slack.messages = [brief(1)]
        self.call("slack-message", request_id="manual", message_ts=ts(1))
        self.relay.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "already_sent")

    def test_explicit_repeat_requires_new_id_and_reason(self):
        self.slack.messages = [brief(1)]
        self.call("slack-message", request_id="first", message_ts=ts(1))
        repeated = self.call("slack-message", request_id="second", message_ts=ts(1))
        self.assertEqual(repeated["status"], "already_sent")
        with self.assertRaises(r.RelayError):
            self.call("slack-message", request_id="bad-repeat", message_ts=ts(1), allow_repeat=True)
        self.call("slack-message", request_id="approved-repeat", message_ts=ts(1),
                  allow_repeat=True, reason="explicitly requested")
        self.assertEqual(len(self.wecom.sent), 2)

    def test_unknown_never_retries_even_after_restart(self):
        self.wecom.results = [TimeoutError("private payload must not escape")]
        result = self.call(request_id="timeout", text="hello")
        self.assertEqual(result["status"], "unknown")
        self.store.db.close()
        self.store = r.Store(self.root / "state")
        self.relay.store = self.store
        again = self.call(request_id="timeout", text="hello")
        self.assertEqual(again["status"], "unknown")
        self.assertEqual(len(self.wecom.sent), 1)

    def test_inflight_recovery_is_unknown(self):
        self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('crash','hash','auto',0,'sending')")
        self.store.db.execute("INSERT INTO parts VALUES ('crash',0,'hash','sending')")
        self.store.db.commit()
        self.store.db.close()
        self.store = r.Store(self.root / "state")
        self.assertEqual(self.store.status("crash")["status"], "unknown")
        self.assertEqual(self.store.db.execute("SELECT status FROM parts").fetchone()[0], "unknown")

    def test_chunk_bounds_pacing_and_partial_unknown(self):
        text = "中文🙂" * 2000
        pieces = r.chunks(text)
        self.assertTrue(all(len(p.encode()) <= 2048 for p in pieces))
        self.wecom.results = ["sent", "unknown"]
        result = self.call(request_id="partial", text=text)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["parts_sent"], 1)
        self.assertEqual(len(self.wecom.sent), 2)
        self.assertGreaterEqual(self.now, 1003.2)

    def test_audit_has_no_body(self):
        sentinel = "PRIVATE_BODY_DO_NOT_PERSIST_13493732"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.call(request_id="audit", text=sentinel)
        self.assertNotIn(sentinel, output.getvalue())
        for path in (self.root / "state").iterdir():
            self.assertNotIn(sentinel.encode(), path.read_bytes())

    def test_input_and_uid_boundaries(self):
        with self.assertRaises(r.RelayError) as caught:
            self.relay.dispatch("/manual/text", {"request_id": "one", "text": "x"}, os.getuid() + 1)
        self.assertEqual(caught.exception.status, 403)
        for payload in ({"request_id": "auto:bad", "text": "x"},
                        {"request_id": "bad", "text": "x", "webhook": "arbitrary"},
                        {"request_id": "huge", "text": "x" * (r.MAX_BODY + 1)}):
            with self.assertRaises(r.RelayError):
                self.call(**payload)
        self.assertEqual(self.wecom.sent, [])

    def test_secure_config_and_file_boundaries(self):
        self.assertEqual(r.load_config(self.root), CONFIG)
        self.config_path.chmod(0o644)
        with self.assertRaises(r.RelayError):
            r.load_config(self.root)
        self.config_path.chmod(0o600)
        link = self.root / "hardlink"
        os.link(self.config_path, link)
        with self.assertRaises(r.RelayError):
            r.secure_read(self.config_path)
        link.unlink()
        link.symlink_to(self.config_path)
        with self.assertRaises(OSError):
            r.secure_read(link)
        (self.root / "secrets").chmod(0o755)
        with self.assertRaises(r.RelayError):
            r.load_config(self.root)

    def test_second_process_lock_rejected(self):
        with r.exclusive(self.root):
            with self.assertRaises(r.RelayError) as caught:
                with r.exclusive(self.root):
                    self.fail("duplicate service lock admitted")
        self.assertEqual(caught.exception.code, "already_running")

    def test_unix_http_peer_and_json_no_network(self):
        path = self.root / "run" / "test.sock"
        server = socketserver.UnixStreamServer(str(path), r.Handler)
        path.chmod(0o600)
        server.relay = self.relay
        results = []

        def client():
            connection = r.UnixConnection(path)
            connection.request("POST", "/manual/text",
                               body=json.dumps({"request_id": "socket", "text": "hello"}),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            results.append((response.status, json.loads(response.read())))
            connection.close()

        thread = threading.Thread(target=client)
        thread.start()
        try:
            server.handle_request()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0][0], 200)
            self.assertEqual(results[0][1]["status"], "sent")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            server.server_close()


class TransportTests(unittest.TestCase):
    def test_slack_identity_and_scope_boundaries(self):
        slack = r.Slack("test-not-a-credential", CONFIG)
        identity = {"team_id": "TTEST", "user_id": "UOWNER"}
        with patch.object(slack, "call", return_value=(identity, "im:history")):
            slack.verify()
        with patch.object(slack, "call", return_value=(identity, "identify,im:history")):
            slack.verify()
        for body, scope in (({**identity, "team_id": "TOTHER"}, "im:history"),
                            ({**identity, "bot_id": "BOT"}, "im:history"),
                            (identity, "im:history,chat:write"), (identity, "identify"), (identity, "")):
            with patch.object(slack, "call", return_value=(body, scope)):
                with self.assertRaises(r.RelayError):
                    slack.verify()

    def test_exact_lookup_never_returns_adjacent_message(self):
        slack = r.Slack("test-not-a-credential", CONFIG)
        with patch.object(slack, "history", return_value={"messages": [brief(2)]}):
            with self.assertRaises(r.RelayError):
                slack.exact(ts(1))

    def test_wecom_unknown_for_exception_and_invalid_response(self):
        transport = r.WeCom("https://example.invalid/unused")
        with patch.object(transport.http, "open", side_effect=TimeoutError("private-token-sentinel")):
            self.assertEqual(transport.send("private-body-sentinel"), "unknown")

    def test_wecom_only_confirms_explicit_success(self):
        transport = r.WeCom("https://example.invalid/unused")
        for code, expected in ((0, "sent"), (45009, "failed"), (None, "unknown")):
            response = io.BytesIO(json.dumps({"errcode": code}).encode())
            response.status = 200
            with patch.object(transport.http, "open", return_value=response):
                self.assertEqual(transport.send("body"), expected)

    def test_reject_wrong_token_type_and_redirect(self):
        with self.assertRaises(r.RelayError):
            r.validate_credentials({"slack_user_token": "xoxb-fake", "wecom_webhook": "unused"})
        with self.assertRaises(r.RelayError):
            r.NoRedirect().redirect_request(None)

    def test_plain_text_does_not_fetch_links(self):
        self.assertEqual(r.plain_text("<https://example.invalid|Title> &amp; more"),
                         "Title (https://example.invalid) & more")


class RenderingTests(unittest.TestCase):
    def test_redacted_real_structure_retains_whitespace_node(self):
        message = redacted_observed_message()
        self.assertEqual(message["text"].count("\n"), 0)
        rendered = r.message_text(message)
        self.assertEqual(rendered, "AI Infra 晨报\n\n脱敏测试正文，不包含用户原始文本。")
        wire = json.dumps({"msgtype": "text", "text": {"content": rendered}}, ensure_ascii=False)
        self.assertEqual(json.loads(wire)["text"]["content"], rendered)

    def test_inline_text_styles_and_links_are_not_split(self):
        message = {"text": "fallback", "blocks": [{"type": "rich_text", "elements": [
            rich_section(text_node("Read "),
                         {"type": "text", "text": "this", "style": {"bold": True}},
                         text_node(" "),
                         {"type": "link", "url": "https://example.invalid/paper", "text": "paper"},
                         text_node(".\nNext line."))]}]}
        self.assertEqual(r.message_text(message),
                         "Read this paper (https://example.invalid/paper).\nNext line.")

    def test_full_brief_paragraphs_lists_quotes_code_and_links(self):
        message = {"text": "fallback", "blocks": [{"type": "rich_text", "elements": [
            rich_section(text_node("AI Infra 晨报")),
            rich_section(text_node("方案一\n问题、收益与限制。")),
            {"type": "rich_text_list", "style": "ordered", "offset": 2, "elements": [
                rich_section(text_node("第一项")),
                rich_section({"type": "link", "url": "https://example.invalid"})]},
            {"type": "rich_text_quote", "elements": [text_node("引用第一行\n引用第二行")]},
            {"type": "rich_text_preformatted", "elements": [text_node("a -> b\nb -> c")]},
            rich_section(text_node("结论："), {"type": "emoji", "name": "white_check_mark"})]}]}
        rendered = r.message_text(message)
        self.assertEqual(rendered,
                         "AI Infra 晨报\n\n方案一\n问题、收益与限制。\n\n"
                         "3. 第一项\n4. https://example.invalid\n\n"
                         "> 引用第一行\n> 引用第二行\n\n```\na -> b\nb -> c\n```\n\n"
                         "结论：:white_check_mark:")

    def test_plain_text_and_empty_blocks_fall_back(self):
        text = "one\n\ntwo <https://example.invalid|link>"
        expected = "one\n\ntwo link (https://example.invalid)"
        for blocks in (None, [], [{"type": "rich_text", "elements": []}]):
            self.assertEqual(r.message_text({"text": text, "blocks": blocks}), expected)
        self.assertEqual(r.message_text({"text": text}), expected)

    def test_malformed_or_unsupported_blocks_fall_back_as_a_whole(self):
        for blocks in ([{"type": "rich_text", "elements": "wrong"}],
                       [{"type": "rich_text", "elements": [rich_section(text_node("partial"),
                           {"type": "unknown", "text": "DO_NOT_FORWARD"})]}],
                       [{"type": "rich_text", "elements": [None]}],
                       [{"type": "section", "text": {"type": "mrkdwn", "text": None}}],
                       [{"type": "section", "fields": None}],
                       [{"type": "actions", "elements": [{"text": "DO_NOT_FORWARD"}]}]):
            self.assertEqual(r.message_text({"text": "safe fallback", "blocks": blocks}), "safe fallback")

    def test_does_not_collect_metadata_accessories_or_attachments(self):
        message = {"text": "fallback", "metadata": {"text": "PRIVATE_METADATA"},
                   "attachments": [{"text": "PRIVATE_ATTACHMENT"}], "blocks": [{
                       "type": "section", "block_id": "PRIVATE_BLOCK_ID",
                       "text": {"type": "mrkdwn", "text": "Visible\ntext"},
                       "accessory": {"text": "PRIVATE_ACCESSORY", "url": "https://example.invalid/private"}}]}
        self.assertEqual(r.message_text(message), "Visible\ntext")

    def test_header_and_section_text_and_visible_fields(self):
        message = {"text": "fallback", "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "AI Infra 晨报"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "<https://example.invalid|Source>"},
             "fields": [{"type": "plain_text", "text": "Result: faster"}]},
            {"type": "divider"}]}
        self.assertEqual(r.message_text(message),
                         "AI Infra 晨报\n\nSource (https://example.invalid)\n\nResult: faster\n\n---")

    def test_paragraph_boundaries_do_not_add_to_existing_blank_lines(self):
        message = {"text": "fallback", "blocks": [{"type": "rich_text", "elements": [
            rich_section(text_node("first\n")), rich_section(text_node("\nsecond\n\n")),
            rich_section(text_node("third"))]}]}
        self.assertEqual(r.message_text(message), "first\n\nsecond\n\nthird")

    def test_multibyte_parts_reassemble_without_loss(self):
        body = ("中文🙂\n" * 1500) + "\nEnd"
        message = {"text": "fallback", "blocks": [{"type": "rich_text", "elements": [
            rich_section(text_node("AI Infra 晨报\n\n"), text_node(body))]}]}
        rendered = r.message_text(message)
        parts = r.chunks(rendered)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part.encode()) <= 2048 for part in parts))
        self.assertEqual("".join(part.split("] ", 1)[1] for part in parts), rendered)
        with self.assertRaises(r.RelayError):
            r.chunks(r.message_text({"blocks": [{"type": "rich_text", "elements": [
                rich_section(text_node("x" * (r.MAX_BODY + 1)))]}]}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
