"""认证回读桥接测试；仅用合成 Slack 和 Pages 数据，不执行写入。"""
from copy import deepcopy
from contextlib import nullcontext, redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
from unittest.mock import patch
import unittest

import notice_check as check
import relay
from test_article_notice import manifest, message, metadata_png
from test_images import PNG
from test_notice_connector import PROFILE, NOTICE_CONFIG


class Reader:
    def __init__(self, value):
        self.value, self.reads = deepcopy(value), 0

    def read(self, notice):
        self.reads += 1
        return deepcopy(self.value)


class Slack:
    def __init__(self):
        self.config = {**deepcopy(NOTICE_CONFIG), "user_id": PROFILE["user_id"]}
        self.message, self.poster = message(), PNG
        self.verified, self.reads, self.downloads = False, 0, 0
        self.body_changes, self.after_download = {}, None

    def verify(self):
        self.verified = True

    def history(self, **params):
        assert self.verified
        assert params == {"oldest": self.message["ts"], "latest": self.message["ts"], "inclusive": "true", "limit": 2}
        self.reads += 1
        return {"messages": [deepcopy(self.message)], "has_more": False, **self.body_changes}

    def download_image(self, plan):
        assert self.verified and plan["file_id"] == self.message["files"][0]["id"]
        self.downloads += 1
        if self.after_download:
            self.after_download()
        return self.poster


class NoticeReadTests(unittest.TestCase):
    def setUp(self):
        self.slack = Slack()
        self.request = {"message_ts": self.slack.message["ts"], "file_id": self.slack.message["files"][0]["id"], "notice_manifest": manifest()}
        self.reader = Reader(manifest())

    def run_check(self):
        return check.read_received_notice(json.dumps(self.request).encode(), self.slack.config, self.slack, notice_reader=self.reader)

    def test_reads_real_checker_and_reports_only_observed_slack_receipt(self):
        result = self.run_check()
        self.assertEqual(result["slack_status"], "verified")
        self.assertEqual(result["wecom_status"], "not_checked")
        self.assertEqual(result["publication_status"], "not_checked")
        self.assertEqual(result["receipt"]["transport_bytes"], len(PNG))
        self.assertEqual(result["receipt"]["transport_sha256"], manifest()["poster"]["sha256"])
        self.assertEqual(result["receipt"]["file_id"], self.request["file_id"])
        self.assertFalse(result["state_changed"])
        self.assertEqual(result["messages_sent"], 0)
        self.assertEqual((self.slack.reads, self.slack.downloads, self.reader.reads), (2, 1, 2))

    def test_later_deployment_preserves_exact_report_content_binding(self):
        self.reader.value["commit"] = "c" * 40
        self.assertEqual(self.run_check()["observed_publication_commit"], "c" * 40)
        for key in ("report_sha256", "poster"):
            with self.subTest(key=key):
                self.reader.value = manifest()
                if key == "poster":
                    self.reader.value[key]["sha256"] = "c" * 64
                else:
                    self.reader.value[key] = "c" * 64
                with self.assertRaisesRegex(check.NoticeCheckError, "content differs"):
                    self.run_check()

    def test_original_bytes_required_even_when_only_png_metadata_changes(self):
        self.slack.poster = metadata_png()
        self.slack.message["files"][0]["size"] = len(self.slack.poster)
        with self.assertRaisesRegex(check.NoticeCheckError, "exactly match published"):
            self.run_check()

    def test_untrusted_identity_and_file_are_rejected_before_download(self):
        for mutation in ({"app_id": "AOTHER"}, {"user": "UOTHER"}, {"bot_id": "BOTHER"}, {"edited": {}}, {"thread_ts": "1793400000.000001"}):
            with self.subTest(mutation=mutation):
                self.slack.message = {**message(), **mutation}
                with self.assertRaises(relay.RelayError):
                    self.run_check()
        self.slack.message = message()
        self.request["file_id"] = "FOTHER"
        with self.assertRaisesRegex(check.NoticeCheckError, "reserved file"):
            self.run_check()
        self.assertEqual(self.slack.downloads, 0)

    def test_partial_or_ambiguous_history_never_becomes_evidence(self):
        for changes in ({"has_more": True}, {"response_metadata": {"next_cursor": "next"}}, {"messages": []}, {"messages": [message(), message()]}):
            with self.subTest(changes=changes):
                self.slack.body_changes = changes
                with self.assertRaisesRegex(check.NoticeCheckError, "unique exact-message"):
                    self.run_check()
        self.assertEqual(self.slack.downloads, 0)

    def test_edited_message_or_changed_publication_during_download_rejected(self):
        for change in (lambda: self.slack.message.update(edited={}), lambda: self.reader.value.update(commit="d" * 40)):
            self.slack.message = message()
            self.reader.value = manifest()
            self.slack.after_download = change
            with self.assertRaisesRegex(check.NoticeCheckError, "changed during"):
                self.run_check()

    def test_request_cannot_supply_identity_destination_or_read_success(self):
        for key in ("read_context", "channel_id", "sender_id", "complete", "bytes"):
            with self.subTest(key=key):
                request = {**self.request, key: "forged"}
                with self.assertRaises(check.NoticeCheckError):
                    check.read_received_notice(json.dumps(request).encode(), self.slack.config, self.slack)
        self.assertFalse(self.slack.verified)
        with self.assertRaises(check.NoticeCheckError):
            check.read_received_notice(b" " * (relay.MAX_BODY + 1), self.slack.config, self.slack)

    def test_auth_failure_stops_all_metadata_reads_and_downloads(self):
        with patch.object(self.slack, "verify", side_effect=relay.RelayError("wrong_slack_identity")):
            with self.assertRaises(relay.RelayError):
                self.run_check()
        self.assertEqual((self.slack.reads, self.slack.downloads), (0, 0))

    def test_reader_failure_is_not_a_verified_receipt_or_retry_write(self):
        with patch.object(self.reader, "read", side_effect=check.notice_format.NoticeReadError("read_failed")):
            with self.assertRaises(check.notice_format.NoticeReadError):
                self.run_check()
        self.assertEqual(self.slack.downloads, 0)

    def test_cli_rejects_input_before_secrets_and_never_opens_a_store(self):
        output = io.StringIO()
        with patch.object(relay, "load_config", return_value=self.slack.config), patch.object(relay, "load_secrets") as secrets, patch.object(relay, "Store") as store, patch.object(check.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{}'))), redirect_stdout(output):
            self.assertEqual(check.main(["--root", str(Path("synthetic"))]), 1)
        secrets.assert_not_called()
        store.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["slack_status"], "not_verified")

    def test_cli_sanitizes_errors_instead_of_returning_transport_secrets(self):
        output = io.StringIO()
        with patch.object(relay, "load_config", side_effect=RuntimeError("private transport detail")), redirect_stdout(output):
            self.assertEqual(check.main(["--root", "synthetic"]), 1)
        self.assertNotIn("private transport", output.getvalue())

    def test_cli_success_returns_receipt_without_opening_delivery_writers(self):
        output = io.StringIO()
        with patch.object(relay, "load_config", return_value=self.slack.config), patch.object(relay, "load_secrets", return_value={"slack_user_token": "synthetic-token"}), patch.object(relay, "Slack", return_value=self.slack), patch.object(relay, "Store") as store, patch.object(relay, "WeCom") as wecom, patch.object(check.notice_format, "NoticeManifestReader", return_value=self.reader), patch.object(check.sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(self.request).encode()))), redirect_stdout(output):
            self.assertEqual(check.main(["--root", "synthetic"]), 0)
        store.assert_not_called()
        wecom.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["slack_status"], "verified")
        self.assertEqual(result["messages_sent"], 0)
        self.assertNotIn("synthetic-token", output.getvalue())
        self.assertNotIn("blocks", result)

    def test_actual_slack_client_uses_only_get_and_existing_read_scopes(self):
        calls = []
        config = self.slack.config
        observed = self.slack.message

        class Response(io.BytesIO):
            status = 200

            def __init__(self, data, url, mime, scopes=""):
                super().__init__(data)
                self.url = url
                self.headers = Message()
                self.headers["Content-Type"] = mime
                self.headers["Content-Length"] = str(len(data))
                self.headers["x-oauth-scopes"] = scopes

            def geturl(self):
                return self.url

        class HTTP:
            def open(self, request, timeout):
                self_outer.assertEqual(request.get_method(), "GET")
                calls.append(request.full_url)
                if "/auth.test" in request.full_url:
                    return Response(json.dumps({"ok": True, "team_id": config["team_id"], "user_id": config["user_id"]}).encode(), request.full_url, "application/json", "im:history,files:read,identify")
                if "/conversations.history?" in request.full_url:
                    return Response(json.dumps({"ok": True, "messages": [observed], "has_more": False}).encode(), request.full_url, "application/json")
                self_outer.assertTrue(request.full_url.startswith("https://files.slack.com/files-pri/"))
                return Response(PNG, request.full_url, "image/png")

        self_outer = self
        client = relay.Slack("synthetic-token", config)
        client.http = HTTP()
        with patch.object(relay, "image_download_timeout", nullcontext):
            result = check.read_received_notice(json.dumps(self.request).encode(), config, client, notice_reader=self.reader)
        self.assertEqual(result["slack_status"], "verified")
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
