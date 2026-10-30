"""仅使用离线合成图片和脱敏后的生产附件结构。"""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import images as im
import relay as r
from test_relay import CONFIG, FakeSlack, FakeWeCom, brief, ts


def png(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\0\0\0\xff")) + chunk(b"IEND", b""))


PNG = png()
# 用于结构校验的 JPEG 测试样本，不代表完整的编解码或像素验证。
JPEG = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9")


def attachment(file_id="FTEST1", **changes):
    # 字段与观察到的托管 PNG 相同；已移除真实名称、标识符和 URL。
    return {"id": file_id, "mimetype": "image/png", "filetype": "png", "size": len(PNG),
            "mode": "hosted", "is_external": False, "file_access": "visible", "user_team": "TTEST",
            "url_private": "https://files.slack.com/files-pri/TTEST-" + file_id + "/test.png", **changes}


class Response(io.BytesIO):
    def __init__(self, data=PNG, url=None, headers=None, status=200):
        super().__init__(data)
        self.url = url or attachment()["url_private"]
        self.headers = {"Content-Type": "image/png", "Content-Length": str(len(data))} if headers is None else headers
        self.status = status

    def geturl(self):
        return self.url


class ImageFormatTests(unittest.TestCase):
    def test_png_and_jpeg(self):
        self.assertEqual(im.validate_image(PNG, "image/png"), (1, 1))
        self.assertEqual(im.validate_image(JPEG, "image/jpeg"), (1, 1))

    def test_corrupt_truncated_mislabeled_or_oversize_image(self):
        for data, mime in ((b"<html>login</html>", "image/png"), (PNG[:-1], "image/png"),
                           (PNG[:40] + b"X" + PNG[41:], "image/png"), (PNG, "image/jpeg"),
                           (JPEG[:-1], "image/jpeg"), (JPEG[:6], "image/jpeg"),
                           (PNG + b"trailing-private-data", "image/png"), (PNG, "image/gif"),
                           (b"x" * (im.MAX_IMAGE_BYTES + 1), "image/png")):
            with self.assertRaises(im.ImageError):
                im.validate_image(data, mime)

    def test_dimension_bomb_rejected_without_decompression(self):
        with self.assertRaises(im.ImageError) as caught:
            im.validate_image(png(16000, 16000), "image/png")
        self.assertEqual(caught.exception.code, "image_dimensions_rejected")

    def test_only_selected_slack_file_urls(self):
        good = attachment()["url_private"]
        self.assertEqual(im.image_url(good, "TTEST", "FTEST1"), good)
        for bad in (good.replace("https:", "http:"), good.replace("files.slack.com", "files.slack.com.attacker.invalid"),
                     good.replace("files.slack.com", "localhost"), good.replace("files.slack.com", "files.slack.com@localhost"),
                    good.replace("files.slack.com", "files.slack.com:443"), good.replace("FTEST1", "FOTHER"),
                    good.replace("TTEST", "TOTHER"), good + "#fragment", good + "\n",
                    good.replace("test.png", "../other.png"), good.replace("test.png", "%2e%2e/other.png"),
                    good.replace("test.png", "%252e%252e/other.png")):
            with self.assertRaises(im.ImageError):
                im.image_url(bad, "TTEST", "FTEST1")

    def test_select_images_metadata_dedup_and_boundaries(self):
        plans = im.select_images({"files": [attachment(), attachment(), {"mimetype": "application/pdf"}]}, "TTEST")
        self.assertEqual(len(plans), 1)
        self.assertNotIn("error", plans[0])
        for item in (attachment(is_external=True), attachment(mode="external"),
                     attachment(file_access="check_file_info"), attachment(user_team="TOTHER"),
                     attachment(size=im.MAX_IMAGE_BYTES + 1), attachment(size=True),
                     attachment(mimetype="image/gif", filetype="gif"), attachment(filetype="html")):
            self.assertIn("error", im.select_images({"files": [item]}, "TTEST")[0])
        too_many = im.select_images({"files": [attachment("FTEST" + str(n)) for n in range(im.MAX_IMAGES + 1)]}, "TTEST")
        self.assertEqual(too_many[0]["error"], "too_many_images")


class ImageTransportTests(unittest.TestCase):
    def slack(self):
        slack = r.Slack("synthetic-token-not-a-secret", {**CONFIG, "forward_images": True})
        identity = {"team_id": CONFIG["team_id"], "user_id": CONFIG["user_id"]}
        with patch.object(slack, "call", return_value=(identity, "identify,im:history,files:read")):
            slack.verify()
        return slack

    def test_image_scope_is_opt_in_and_required(self):
        identity = {"team_id": CONFIG["team_id"], "user_id": CONFIG["user_id"]}
        for enabled, scopes in ((False, "im:history,files:read"), (True, "im:history"),
                                (True, "im:history,files:read,files:write")):
            slack = r.Slack("synthetic", {**CONFIG, "forward_images": enabled})
            with patch.object(slack, "call", return_value=(identity, scopes)):
                with self.assertRaises(r.RelayError):
                    slack.verify()
            with patch.object(slack.http, "open") as network:
                with self.assertRaises(im.ImageError):
                    slack.download_image(im.validate_file(attachment(), "TTEST"))
                network.assert_not_called()

    def test_download_uses_auth_only_on_validated_host(self):
        slack = self.slack()
        plan = im.validate_file(attachment(), "TTEST")
        with patch.object(slack.http, "open", return_value=Response()) as network:
            self.assertEqual(slack.download_image(plan), PNG)
            request = network.call_args.args[0]
            self.assertEqual(request.full_url, plan["url"])
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token-not-a-secret")
            self.assertEqual(request.get_header("Accept-encoding"), "identity")
        with patch.object(slack.http, "open") as network:
            with self.assertRaises(im.ImageError):
                slack.download_image({**plan, "url": "https://attacker.invalid/private"})
            network.assert_not_called()

    def test_download_rejects_http_html_encoding_redirect_and_size_errors(self):
        plan = im.validate_file(attachment(), "TTEST")
        responses = [Response(status=403), Response(url="https://attacker.invalid"),
                     Response(headers={"Content-Type": "text/html"}),
                     Response(headers={"Content-Type": "image/png", "Content-Encoding": "gzip"}),
                     Response(headers={"Content-Type": "image/png", "Content-Length": "999999999"}),
                     Response(headers={"Content-Type": "image/png", "Content-Length": "invalid"}),
                     Response(PNG[:-1]), Response(PNG + b"x", headers={"Content-Type": "image/png"})]
        for response in responses:
            slack = self.slack()
            with patch.object(slack.http, "open", return_value=response):
                with self.assertRaises(im.ImageError):
                    slack.download_image(plan)
        slack = self.slack()
        for error in (TimeoutError("DO_NOT_LEAK"), r.RelayError("redirect_rejected")):
            with patch.object(slack.http, "open", side_effect=error):
                with self.assertRaises(im.ImageError) as caught:
                    slack.download_image(plan)
                self.assertEqual(str(caught.exception), "image_download_failed")

    def test_download_without_content_length_and_total_time_limit(self):
        slack = self.slack()
        plan = im.validate_file(attachment(), "TTEST")
        with patch.object(slack.http, "open", return_value=Response(headers={"Content-Type": "application/octet-stream"})):
            self.assertEqual(slack.download_image(plan), PNG)
        with patch.object(slack.http, "open", return_value=Response()), patch.object(r.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaises(im.ImageError) as caught:
                slack.download_image(plan)
            self.assertEqual(caught.exception.code, "image_download_timed_out")

    def test_image_payload_and_acknowledgement(self):
        transport = r.WeCom("https://example.invalid/unused")
        response = Response(b'{"errcode":0}', headers={})
        with patch.object(transport.http, "open", return_value=response) as network:
            self.assertEqual(transport.send_image(PNG, "image/png"), "sent")
            body = json.loads(network.call_args.args[0].data)
            self.assertEqual(set(body), {"msgtype", "image"})
            self.assertEqual(body["msgtype"], "image")
            self.assertEqual(base64.b64decode(body["image"]["base64"], validate=True), PNG)
            self.assertEqual(body["image"]["md5"], hashlib.md5(PNG).hexdigest())
        with patch.object(transport.http, "open") as network:
            with self.assertRaises(im.ImageError):
                transport.send_image(b"bad", "image/png")
            network.assert_not_called()
        for response_body, expected in ((b'{"errcode":false}', "unknown"),
                                         (b'{"errcode":45009}', "failed"), (b'not-json', "unknown")):
            with patch.object(transport.http, "open", return_value=Response(response_body, headers={})):
                self.assertEqual(transport.send_image(PNG, "image/png"), expected)


class ImageRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-images-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = r.Store(self.root)
        self.config = {**CONFIG, "forward_images": True}
        self.slack, self.wecom = FakeSlack(), FakeWeCom()
        self.downloads, self.sent_images, self.order = [], [], []
        self.image_results = []
        self.now = 1000.0
        self.slack.download_image = self.download
        self.wecom.send_image = self.send_image
        original_send = self.wecom.send

        def send_text(text):
            self.order.append("text")
            return original_send(text)
        self.wecom.send = send_text
        self.relay = r.Relay(self.store, self.slack, self.wecom, self.config,
                             clock=lambda: self.now, sleeper=self.advance)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def advance(self, seconds):
        self.now += seconds

    def download(self, plan):
        self.downloads.append(plan["file_id"])
        return PNG

    def send_image(self, data, mime):
        self.order.append("image")
        self.sent_images.append((data, mime))
        result = self.image_results.pop(0) if self.image_results else "sent"
        if isinstance(result, Exception):
            raise result
        return result

    def poll_one(self, files=None, **changes):
        self.relay.baseline()
        self.slack.messages.append(brief(1, files=files if files is not None else [attachment()], **changes))
        self.relay.poll()
        return self.store.status("auto:" + ts(1))

    def test_text_then_image_count_and_combined_pacing(self):
        result = self.poll_one()
        self.assertEqual(self.order, ["text", "image"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual((result["parts_total"], result["parts_sent"]), (2, 2))
        self.assertEqual(result["images"][0]["status"], "sent")
        self.assertGreaterEqual(self.now, 1003.2)
        self.relay.poll()
        self.assertEqual(len(self.sent_images), 1)

    def test_images_disabled_and_nonbrief_sender_thread_boundaries(self):
        self.relay.config = CONFIG
        self.poll_one()
        self.assertEqual(self.downloads, [])
        self.relay.config = self.config
        for changes in ({"text": "Ordinary chat"}, {"user": "UOTHER"},
                        {"thread_ts": ts(0)}, {"edited": {"ts": ts(2)}}):
            message = brief(2, files=[attachment()], **changes)
            self.assertEqual(self.relay.image_plans(message), [])
        self.assertEqual(self.relay.image_plans(brief(2, files=[{"mimetype": "application/pdf"}])), [])

    def test_rejected_attachment_is_partial_but_text_survives(self):
        result = self.poll_one([attachment(is_external=True), attachment("FTEST2")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["images"][0]["error"], "image_not_slack_hosted")
        self.assertEqual(self.downloads, ["FTEST2"])
        self.assertEqual(len(self.wecom.sent), 1)

    def test_download_failure_is_recorded_without_post(self):
        self.slack.download_image = lambda plan: (_ for _ in ()).throw(im.ImageError("image_download_failed"))
        result = self.poll_one()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["parts_sent"], 1)
        self.assertEqual(self.sent_images, [])

    def test_unknown_image_stops_remaining_and_never_retries(self):
        self.image_results = [TimeoutError("private URL not logged")]
        result = self.poll_one([attachment(), attachment("FTEST2")])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual([x["status"] for x in result["images"]], ["unknown", "not_attempted"])
        self.assertEqual(len(self.sent_images), 1)
        self.relay.poll()
        manual = self.relay.dispatch("/manual/slack-message", {"request_id": "no-duplicate", "message_ts": ts(1)}, os.getuid())
        self.assertEqual(manual["status"], "source_already_attempted")
        self.assertEqual(len(self.sent_images), 1)

    def test_text_failure_does_not_download_or_send_image(self):
        self.wecom.results = ["unknown"]
        result = self.poll_one()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["images"][0]["status"], "not_attempted")
        self.assertEqual(self.downloads, [])

    def test_duplicate_attachment_id_and_manual_request_id(self):
        message = brief(1, files=[attachment(), attachment()])
        self.slack.messages = [message]
        payload = {"request_id": "manual", "message_ts": ts(1)}
        first = self.relay.dispatch("/manual/slack-message", payload, os.getuid())
        self.assertEqual(self.relay.dispatch("/manual/slack-message", payload, os.getuid()), first)
        self.assertEqual(self.downloads, ["FTEST1"])
        self.assertEqual(len(self.sent_images), 1)

    def test_no_backfill_after_enabling_images(self):
        message = brief(1, files=[attachment()])
        self.relay.deliver("auto:" + ts(1), {"mode": "auto", "ts": ts(1)},
                           message["text"], ts(1), "auto", os.getuid())
        self.slack.messages = [message]
        self.relay.baseline()
        self.relay.poll()
        result = self.relay.dispatch("/manual/slack-message", {"request_id": "old-report", "message_ts": ts(1)}, os.getuid())
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(self.downloads, [])

    def test_recovery_marks_uncertain_images_without_replaying(self):
        self.store.db.execute("INSERT INTO requests(id,intent_hash,mode,created,status) VALUES ('crash','hash','auto',0,'sending')")
        for index, status in enumerate(("sending", "downloading", "pending")):
            self.store.db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status) VALUES ('crash',?,?,?)", ("FTEST" + str(index), index, status))
        self.store.db.commit()
        self.store.db.close()
        self.store = r.Store(self.root)
        result = self.store.status("crash")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual([x["status"] for x in result["images"]], ["unknown", "not_attempted", "not_attempted"])

    def test_audit_contains_neither_file_bytes_nor_private_url(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.poll_one()
        self.assertEqual(output.getvalue(), "")
        for path in self.root.iterdir():
            data = path.read_bytes()
            self.assertNotIn(PNG, data)
            self.assertNotIn(attachment()["url_private"].encode(), data)
            self.assertNotIn(base64.b64encode(PNG), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
