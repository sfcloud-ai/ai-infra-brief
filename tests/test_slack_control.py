"""只读控制链路的离线回归，故障注入不代表真实 Slack 联调。"""

import copy
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from integrations.slack_control import service as control


CONFIG = {"team_id": "TTEST", "channel_id": "CCONTROL", "responder_user_id": "URELAY",
          "responder_bot_id": "BRELAY", "allowed_users": ["UOWNER"],
          "allowed_bots": [{"app_id": "ADOT", "bot_id": "BDOT"}], "poll_seconds": 60}
NOW = 1800000000.0


def message(command="ping", request_id="req-1", ts=NOW - 1, **extra):
    return {"type": "message", "ts": f"{ts:.6f}", "text": f"!ai-brief {request_id} {command}",
            "user": "UOWNER", **extra}


class FakeSlack:
    def __init__(self):
        self.messages, self.replies, self.ranges = [], [], []
        self.send_error = None
        self.read_error = None
        self.channel_error = None

    def verify_channel(self):
        if self.channel_error:
            raise self.channel_error

    def history(self, oldest, latest):
        self.ranges.append((oldest, latest))
        if self.read_error:
            raise self.read_error
        return sorted([item for item in self.messages if float(oldest) < float(item["ts"]) < float(latest)],
                      key=lambda item: float(item["ts"]))

    def reply(self, receipt):
        self.replies.append(receipt)
        if self.send_error:
            raise self.send_error
        return f"{NOW + len(self.replies):.6f}"


@unittest.skipUnless(hasattr(signal, "setitimer"), "需要 Linux 单线程硬超时")
class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for part in ("state", "run", "secrets"):
            (self.root / part).mkdir(mode=0o700)
        self.mask = os.umask(0o077)
        self.addCleanup(os.umask, self.mask)
        self.now = NOW
        self.slack = FakeSlack()
        self.config = copy.deepcopy(CONFIG)
        self.open()

    def open(self):
        self.store = control.Store(self.root, self.config)
        self.addCleanup(self.store.db.close)
        self.controller = control.Controller(self.store, self.slack, self.config,
                                             clock=lambda: self.now, sleeper=self.sleep)

    def sleep(self, seconds):
        self.now += seconds

    def row(self, ts=NOW - 1):
        return self.store.db.execute("SELECT * FROM requests WHERE source_ts=?", (f"{ts:.6f}",)).fetchone()

    def test_all_commands_poll_and_structured_thread_receipts(self):
        self.controller.poll()
        self.now += 60
        self.slack.messages = [message(command, f"id-{i}", NOW + i + 1) for i, command in enumerate(control.COMMANDS)]
        self.controller.poll()
        self.assertEqual([reply["command"] for reply in self.slack.replies], list(control.COMMANDS))
        self.assertTrue(all(reply["status"] == "ok" for reply in self.slack.replies))
        self.assertEqual(self.slack.replies[2]["result"]["relay_health"], "not_checked")
        self.assertEqual(self.slack.replies[2]["result"]["brief_pipeline"], "not_connected")
        self.assertTrue(all(row[0] == "sent" for row in self.store.db.execute("SELECT delivery FROM requests")))

    def test_startup_skips_history_restart_preserves_cursor_and_new_requests(self):
        self.slack.messages = [message()]
        self.controller.poll()
        self.assertFalse(self.slack.replies)
        self.store.db.close()
        self.open()
        self.now += 60
        self.slack.messages.append(message(request_id="new", ts=NOW + 1))
        self.controller.poll()
        self.assertEqual([reply["request_id"] for reply in self.slack.replies], ["new"])

    def test_sender_channel_team_edit_thread_and_self_are_ignored(self):
        changes = [{"user": "UOTHER"}, {"channel": "COTHER"}, {"team": "TOTHER"},
                   {"edited": {"ts": "x"}}, {"thread_ts": "1000000000.000000"},
                   {"user": "URELAY"}, {"bot_id": "BRELAY", "app_id": "ADOT"},
                   {"bot_id": "BOTHER", "app_id": "ADOT"}, {"app_id": "ADOT"},
                   {"bot_profile": {}}, {"subtype": "bot_message"}, {"subtype": "message_changed"},
                   {"hidden": True}, {"type": "reaction_added"}]
        for change in changes:
            with self.subTest(change=change):
                self.controller.accept(message(**change))
        self.controller.flush()
        self.assertFalse(self.slack.replies)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_dot_app_pair_is_allowed_without_bot_user_field(self):
        item = message(bot_id="BDOT", app_id="ADOT", subtype="bot_message")
        del item["user"]
        self.controller.accept(item)
        self.controller.flush()
        self.assertEqual(self.slack.replies[0]["status"], "ok")

    def test_unknown_shell_arguments_and_long_input_rejected_without_echo(self):
        texts = ["!ai-brief req-1 deploy", "!ai-brief req-2 ping; whoami", "!ai-brief req-3 status --all",
                 "!ai-brief req-4 $(whoami)", "!ai-brief " + "x" * 500, "!ai-brief req-6 ping\n"]
        with patch.object(self.controller, "execute", side_effect=AssertionError("must not execute")) as execute:
            for i, text in enumerate(texts):
                self.controller.accept(message(ts=NOW - i - 1, text=text))
            self.controller.flush()
        execute.assert_not_called()
        self.assertEqual(len(self.slack.replies), len(texts))
        self.assertTrue(all(reply["status"] == "rejected" for reply in self.slack.replies))
        self.assertNotIn("whoami", json.dumps(self.slack.replies))

    def test_duplicate_source_request_and_conflict_do_not_execute_again(self):
        with patch.object(self.controller, "execute", wraps=self.controller.execute) as execute:
            self.controller.accept(message())
            self.controller.accept(message())
            self.controller.accept(message(ts=NOW - 0.5))
            self.controller.accept(message("status", ts=NOW - 0.25))
            self.controller.flush()
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(self.slack.replies), 2)
        self.assertEqual(self.slack.replies[1]["code"], "request_id_conflict")
        self.assertEqual(json.loads(self.row()["receipt"])["command"], "ping")

    def test_request_id_reuse_by_different_caller_is_conflict(self):
        self.controller.accept(message())
        self.controller.accept(message(ts=NOW - 0.5, bot_id="BDOT", app_id="ADOT"))
        self.controller.flush()
        self.assertEqual(self.slack.replies[1]["code"], "request_id_conflict")

    def test_old_future_and_invalid_timestamps_do_not_execute(self):
        for ts in (NOW - 301, NOW + 1):
            self.controller.accept(message(ts=ts))
        self.controller.accept({**message(), "ts": "NaN"})
        self.controller.flush()
        self.assertFalse(self.slack.replies)

    def test_pending_receipt_expires_without_send(self):
        self.controller.accept(message())
        self.now += 301
        self.controller.flush()
        self.assertFalse(self.slack.replies)
        self.assertEqual(self.row()["delivery"], "expired")

    def test_restart_prepared_sends_once_and_sending_becomes_unknown(self):
        self.controller.accept(message())
        self.controller.accept(message(request_id="req-2", ts=NOW - 2))
        self.store.db.execute("UPDATE requests SET delivery='sending' WHERE request_id='req-2'")
        self.store.db.commit()
        self.store.db.close()
        self.open()
        self.controller.flush()
        self.controller.accept(message())
        self.controller.flush()
        self.assertEqual(len(self.slack.replies), 1)
        self.assertEqual(self.row(NOW - 2)["delivery"], "unknown")

    def test_restart_during_execution_does_not_rerun(self):
        self.controller.accept(message())
        self.store.db.execute("UPDATE requests SET delivery='executing'")
        self.store.db.commit()
        self.store.db.close()
        self.open()
        with patch.object(self.controller, "execute", side_effect=AssertionError("replay")):
            self.controller.accept(message())
            self.controller.flush()
        self.assertEqual(self.slack.replies[0]["status"], "interrupted")

    def test_lost_send_response_is_never_automatically_resent(self):
        self.controller.accept(message())
        self.slack.send_error = TimeoutError("secret must not escape")
        self.controller.flush()
        self.store.db.close()
        self.open()
        self.controller.flush()
        self.controller.accept(message(ts=NOW - 0.5))
        self.controller.flush()
        self.assertEqual(len(self.slack.replies), 1)
        self.assertEqual(self.row()["delivery"], "unknown")

    def test_rate_limit_honors_cooldown_no_retry_of_rejected_send(self):
        self.controller.accept(message())
        self.slack.send_error = control.RateLimited(120)
        with self.assertRaises(control.RateLimited):
            self.controller.flush()
        self.assertEqual(self.row()["delivery"], "failed")
        self.assertEqual(float(self.store.get("next_send")), NOW + 120)
        self.controller.flush()
        self.assertEqual(len(self.slack.replies), 1)

    def test_channel_or_history_failure_does_not_advance_cursor_or_send(self):
        self.store.set("cursor", f"{NOW - 60:.6f}")
        self.slack.messages = [message()]
        for attribute in ("channel_error", "read_error"):
            setattr(self.slack, attribute, control.ControlError("failed"))
            with self.assertRaises(control.ControlError):
                self.controller.poll()
            self.assertEqual(self.store.get("cursor"), f"{NOW - 60:.6f}")
            self.assertFalse(self.slack.replies)
            setattr(self.slack, attribute, None)
        self.controller.poll()
        self.assertEqual(len(self.slack.replies), 1)

    def test_command_deadline_preempts_blocking_handler(self):
        with patch.object(self.controller, "execute", side_effect=lambda command: time.sleep(10)):
            before = time.monotonic()
            self.controller.accept(message())
            self.assertLess(time.monotonic() - before, 4)
        self.controller.flush()
        self.assertEqual(self.slack.replies[0]["status"], "timeout")
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_binding_and_permissions_fail_closed(self):
        self.store.db.close()
        for change in ({"channel_id": "COTHER"}, {"allowed_users": ["UOTHER"]}):
            with self.assertRaisesRegex(control.ControlError, "ledger_binding_changed"):
                control.Store(self.root, {**self.config, **change})
        path = self.root / "state/control.sqlite3"
        path.chmod(0o644)
        with self.assertRaises(security_error()):
            control.Store(self.root, self.config)

    def test_single_instance_lock(self):
        with control.security.exclusive(self.root):
            with self.assertRaises(security_error()):
                with control.security.exclusive(self.root):
                    self.fail("second instance started")


def security_error():
    return control.security.RelayError


class ProtocolTests(unittest.TestCase):
    def test_user_and_responder_ids_accept_u_and_w_prefixes(self):
        for prefix in ("U", "W"):
            config = {**CONFIG, "allowed_users": [prefix + "OWNER"],
                      "responder_user_id": prefix + "RELAY"}
            control.validate_config(config)
            controller = control.Controller(None, None, config)
            self.assertEqual(controller.actor(message(user=prefix + "OWNER")), "user:" + prefix + "OWNER")
            self.assertIsNone(controller.actor(message(user=prefix + "RELAY")))
            slack = control.Slack("test", config)
            body = {"team_id": "TTEST", "user_id": prefix + "RELAY", "bot_id": "BRELAY"}
            with patch.object(slack, "call", return_value=(body, control.SCOPES)), patch.object(slack, "verify_channel"):
                slack.verify()

    def test_config_rejects_self_bot_human_unknown_fields_and_bad_interval(self):
        control.validate_config(copy.deepcopy(CONFIG))
        for change in ({"allowed_users": ["URELAY"]}, {"poll_seconds": True}, {"poll_seconds": 1},
                       {"channel_id": "DPRIVATE"}, {"allowed_users": [], "allowed_bots": []},
                       {"allowed_bots": [{"app_id": "ADOT", "bot_id": "BRELAY"}]}, {"shell": "sh"}):
            with self.subTest(change=change), self.assertRaises(control.ControlError):
                control.validate_config({**CONFIG, **change})

    def test_slack_identity_and_scopes(self):
        slack = control.Slack("test", CONFIG)
        body = {"team_id": "TTEST", "user_id": "URELAY", "bot_id": "BRELAY"}
        with patch.object(slack, "call", return_value=(body, control.SCOPES)), patch.object(slack, "verify_channel"):
            slack.verify()
        for wrong in ({**body, "team_id": "TOTHER"}, {**body, "bot_id": "BDOT"}, {**body, "user_id": "UDOT"}):
            with patch.object(slack, "call", return_value=(wrong, control.SCOPES)), self.assertRaises(control.ControlError):
                slack.verify()
        with patch.object(slack, "call", return_value=(body, {"chat:write"})), self.assertRaises(control.ControlError):
            slack.verify()

    def test_channel_requires_private_local_nonshared_membership(self):
        slack = control.Slack("test", CONFIG)
        channel = {"id": "CCONTROL", "is_private": True, "is_member": True, "is_archived": False,
                   "is_im": False, "is_mpim": False, "is_shared": False, "is_ext_shared": False, "is_org_shared": False}
        with patch.object(slack, "call", return_value=({"channel": channel}, set())):
            slack.verify_channel()
        for change in ({"id": "COTHER"}, {"is_private": False}, {"is_member": False}, {"is_shared": True},
                       {"is_ext_shared": True}, {"is_org_shared": True}, {"is_archived": True}, {"is_im": True}):
            with patch.object(slack, "call", return_value=({"channel": {**channel, **change}}, set())):
                with self.assertRaises(control.ControlError):
                    slack.verify_channel()

    def test_pagination_sorted_and_incomplete_page_rejected(self):
        slack = control.Slack("test", CONFIG)
        pages = [({"messages": [message(ts=NOW - 1)], "has_more": True,
                   "response_metadata": {"next_cursor": "page-2"}}, set()),
                 ({"messages": [message(ts=NOW - 2)], "has_more": False}, set())]
        with patch.object(slack, "call", side_effect=pages) as call:
            result = slack.history(str(NOW - 60), str(NOW))
            self.assertLess(float(result[0]["ts"]), float(result[1]["ts"]))
            self.assertEqual(call.call_args.args[1]["cursor"], "page-2")
        for body in ({"messages": [message()], "has_more": True}, {"messages": [{"ts": "bad"}]}):
            with patch.object(slack, "call", return_value=(body, set())), self.assertRaises(control.ControlError):
                slack.history(str(NOW - 60), str(NOW))

    def test_reply_has_fixed_destination_plain_json_and_verified_response(self):
        slack = control.Slack("test", CONFIG)
        receipt = {"source_ts": f"{NOW - 1:.6f}", "request_id": "req-1"}
        with patch.object(slack, "call", return_value=({"channel": "CCONTROL", "ts": f"{NOW:.6f}"}, set())) as call:
            slack.reply(receipt)
            method, params = call.call_args.args
            self.assertEqual(method, "chat.postMessage")
            self.assertEqual(params["channel"], "CCONTROL")
            self.assertEqual(params["thread_ts"], receipt["source_ts"])
            self.assertEqual(json.loads(params["text"]), receipt)
            self.assertFalse(params["mrkdwn"])
        with patch.object(slack, "call", return_value=({"channel": "COTHER", "ts": f"{NOW:.6f}"}, set())):
            with self.assertRaises(control.ControlError):
                slack.reply(receipt)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "需要 Linux 硬超时")
    def test_http_redaction_rate_limit_response_limit_and_no_redirect(self):
        slack = control.Slack("token-must-not-escape", CONFIG)
        for error in (TimeoutError("token-must-not-escape"), urllib.error.HTTPError("secret", 500, "secret", {}, None)):
            with patch.object(slack.http, "open", side_effect=error), self.assertRaises(control.ControlError) as result:
                slack.call("auth.test", {})
            self.assertNotIn("secret", str(result.exception))
            self.assertNotIn("token", str(result.exception))
        error = urllib.error.HTTPError("secret", 429, "secret", {"Retry-After": "120"}, None)
        with patch.object(slack.http, "open", side_effect=error), self.assertRaises(control.RateLimited) as result:
            slack.call("auth.test", {})
        self.assertEqual(result.exception.seconds, 120)
        response = io.BytesIO(b"x" * (control.MAX_REPLY + 1))
        with patch.object(slack.http, "open", return_value=response), self.assertRaises(control.ControlError):
            slack.call("auth.test", {})
        with self.assertRaises(security_error()):
            control.security.NoRedirect().redirect_request(None, None, None, None, None, "https://example.com")


if __name__ == "__main__":
    unittest.main()
