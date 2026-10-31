"""真实 relay 函数的图文通知回归；所有身份、海报和传输均为合成数据。"""

from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest

from ai_brief.delivery_format import check_received_notice, prepare_notice_preview, title
from ai_brief.storage import BriefError
from integrations.slack_wecom import relay

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/slack_wecom"))
import test_images as image_fixtures
import test_relay as relay_fixtures
sys.path.pop(0)

URL = "https://example.org/reports/2026-10-31/"
SUBTITLE = "独立模型权重下的内存预算与调度"


class DeliveryFormatTests(unittest.TestCase):
    def setUp(self):
        self.plan = prepare_notice_preview("2026-10-31", SUBTITLE, URL, image_fixtures.attachment())
        self.now = 1000.0

    def message(self):
        return {**deepcopy(self.plan["event_template"]), "user": "UDOT", "channel": "DTEST", "team": "TTEST",
                "ts": relay_fixtures.ts(1)}

    def check(self, message):
        return check_received_notice(self.plan, message, channel_id="DTEST", sender_id="UDOT", team_id="TTEST",
                                     read_context={"channel_id": "DTEST", "team_id": "TTEST"})

    def test_subtitle_url_and_one_poster_are_a_single_relay_part(self):
        message = self.message()
        self.assertEqual(message["text"], title("2026-10-31") + " · " + SUBTITLE + "\n\n" + URL)
        self.assertEqual(relay.chunks(relay.message_text(message)), [message["text"]])
        self.assertFalse(self.plan["delivery_allowed"])
        result = self.check(message)
        self.assertEqual(result["wecom_status"], "not_checked")
        self.assertEqual(result["publication_status"], "not_checked")
        self.assertEqual(result["receipt"]["relay_request_id"], "auto:" + message["ts"])

    def test_actual_header_section_and_rich_text_blocks_keep_url_and_poster(self):
        heading = self.message()["text"].split("\n", 1)[0]
        structures = [
            [{"type": "header", "text": {"type": "plain_text", "text": heading}},
             {"type": "section", "text": {"type": "mrkdwn", "text": "<" + URL + ">"}}],
            [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
                {"type": "text", "text": heading + "\n\n"}, {"type": "link", "url": URL}]}]}],
        ]
        for blocks in structures:
            with self.subTest(blocks=blocks):
                message = {**self.message(), "blocks": blocks}
                self.assertEqual(self.check(message)["slack_status"], "verified")
                self.assertIn(URL, relay.message_text(message))

    def test_wrong_sender_title_thread_edited_or_channel_is_rejected(self):
        for change in ({"text": "AI Infra 晨报｜10 月 31 日\n" + URL}, {"user": "UOTHER"},
                       {"thread_ts": relay_fixtures.ts(0)}, {"edited": {"ts": relay_fixtures.ts(2)}},
                       {"channel": "DOTHER"}, {"team": "TOTHER"}, {"hidden": True}):
            with self.subTest(change=change), self.assertRaises(BriefError):
                self.check({**self.message(), **change})

    def test_raw_history_requires_authenticated_read_context(self):
        message = self.message()
        del message["channel"]
        del message["team"]
        self.assertEqual(self.check(message)["slack_status"], "verified")
        with self.assertRaisesRegex(BriefError, "read context"):
            check_received_notice(self.plan, message, channel_id="DTEST", sender_id="UDOT", team_id="TTEST",
                                  read_context={"channel_id": "DOTHER", "team_id": "TTEST"})

    def test_conflicting_or_unsupported_blocks_are_not_visible_body_proof(self):
        for blocks in ([{"type": "section", "text": {"type": "mrkdwn", "text": "详情在线程"}}],
                       [{"type": "section", "text": {"type": "mrkdwn", "text": "只有摘要"}},
                        {"type": "actions", "elements": []}]):
            with self.subTest(blocks=blocks), self.assertRaises(BriefError):
                self.check({**self.message(), "blocks": blocks})
        with self.assertRaisesRegex(BriefError, "complete URL"):
            self.check({**self.message(), "text": self.message()["text"].replace(URL, URL + "wrong/")})

    def test_visible_accessory_and_link_unfurl_are_rejected(self):
        message = self.message()
        changes = [
            {"attachments": [{"text": "额外正文"}]},
            {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message["text"]},
                         "accessory": {"type": "image", "image_url": "https://example.org/extra.png", "alt_text": "额外图片"}}]},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(BriefError, "accessories or attachments"):
                self.check({**message, **change})

    def test_poster_must_be_one_valid_direct_file(self):
        file = image_fixtures.attachment()
        for files in ([], [file, file], [{**file, "mimetype": "image/svg+xml"}],
                      [{**file, "url_private": "https://example.org/image.png"}]):
            with self.subTest(files=files), self.assertRaises(BriefError):
                self.check({**self.message(), "files": files})

    @unittest.skipUnless(os.name == "posix", "relay store requires POSIX; strict Linux CI covers this case")
    def test_poll_transfers_subtitle_url_and_poster_once_without_real_network(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o700)
            store = relay.Store(state)
            self.addCleanup(store.db.close)
            slack, wecom = relay_fixtures.FakeSlack(), relay_fixtures.FakeWeCom()
            sent_images = []
            slack.download_image = lambda plan: image_fixtures.PNG
            wecom.send_image = lambda data, mime: sent_images.append((data, mime)) or "sent"
            receiver = relay.Relay(store, slack, wecom, {**relay_fixtures.CONFIG, "forward_images": True},
                                   clock=lambda: self.now, sleeper=lambda seconds: setattr(self, "now", self.now + seconds))
            receiver.baseline()
            message = self.message()
            slack.messages.append(message)
            receiver.poll()
            receiver.poll()
            self.assertEqual(wecom.sent, [self.plan["event_template"]["text"]])
            self.assertEqual(sent_images, [(image_fixtures.PNG, "image/png")])
            status = store.status("auto:" + message["ts"])
            self.assertEqual(status["status"], "sent")
            self.assertEqual(status["parts_sent"], status["parts_total"])
            self.assertTrue(all(image["status"] == "sent" for image in status["images"]))

    def test_invalid_notice_is_rejected_without_splitting(self):
        file = image_fixtures.attachment()
        for subtitle, url in (("", URL), ("两行\n子标题", URL), ("长" * 100, URL),
                              (SUBTITLE, "http://example.org/report"), (SUBTITLE, URL + "?token=example"),
                              (SUBTITLE, URL + "a" * 1900)):
            with self.subTest(subtitle=subtitle, url=url), self.assertRaises(BriefError):
                prepare_notice_preview("2026-10-31", subtitle, url, file)
        with self.assertRaises(BriefError):
            prepare_notice_preview("2026-10-31", SUBTITLE, URL, {"mimetype": "image/svg+xml"})
        for day in ("2026-02-30", "20261031", None):
            with self.assertRaises(BriefError):
                title(day)


if __name__ == "__main__":
    unittest.main()
