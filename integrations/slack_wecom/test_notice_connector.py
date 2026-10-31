"""固定连接器的通知契约；仅使用合成附件与模拟传输。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

import relay as r
import resume
from test_images import PNG, attachment
from test_relay import CONFIG, FakeSlack, FakeWeCom, brief, ts

PROFILE = {"user_id": "UOWNER", "app_id": "ACONNECTOR",
           "attribution_text": "*Send tool* <@UTOOL>",
           "report_base_url": "https://example.org/reports/", "not_before": ts(0)}
NOTICE_CONFIG = {**CONFIG, "forward_images": True, "notice_connector": PROFILE}


def notice(n=1, revision=1, file_id="FTEST1"):
    heading = "AI Infra 晨报 · 2026-11-01 · Scheduling and memory"
    url = PROFILE["report_base_url"] + "2026-11-01/r" + str(revision) + "/"
    label = url.removeprefix("https://").rstrip("/")
    return {"type": "message", "user": PROFILE["user_id"], "app_id": PROFILE["app_id"], "ts": ts(n),
            "text": heading + "\n\n<" + url + "|" + label + "> " + PROFILE["attribution_text"],
            "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
                {"type": "text", "text": heading}, {"type": "text", "text": "\n\n"},
                {"type": "link", "url": url, "text": label, "from_llm": True, "truncated": True}]}]},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": PROFILE["attribution_text"], "verbatim": False}]}],
            "files": [attachment(file_id, user=PROFILE["user_id"])]}


class NoticeContractTests(unittest.TestCase):
    def parse(self, message):
        return r.connector_notice(message, PROFILE, CONFIG["team_id"], CONFIG["channel_id"])

    def test_observed_structure_has_one_plain_url_and_no_tool_footer(self):
        message = notice()
        result = self.parse(message)
        self.assertEqual(result["text"], "AI Infra 晨报 · 2026-11-01 · Scheduling and memory\n\n" + result["url"])
        self.assertEqual(result["images"][0]["file_id"], "FTEST1")
        nodes = message["blocks"][0]["elements"][0]["elements"]
        nodes[0]["text"] += nodes.pop(1)["text"]
        self.assertEqual(self.parse(message), result)

    def test_untrusted_identity_context_or_replay_is_rejected(self):
        for changes in ({"user": "UOTHER"}, {"app_id": "AOTHER"}, {"app_id": None},
                        {"bot_id": "BOTHER"}, {"bot_id": ""}, {"edited": None}, {"hidden": True},
                        {"thread_ts": ts(0)}, {"ts": ts(0)}, {"ts": "bad"}, {"type": "other"},
                        {"subtype": "message_changed"}, {"channel": "DOTHER"}, {"team": "TOTHER"},
                        {"attachments": [{"text": "extra"}]}):
            with self.subTest(changes=changes), self.assertRaises(r.RelayError):
                self.parse({**notice(), **changes})

    def test_forged_visible_body_and_file_metadata_are_rejected(self):
        def nodes(m):
            return m["blocks"][0]["elements"][0]["elements"]
        mutations = [
            lambda m: m.update(text="forged fallback"),
            lambda m: m["blocks"][1]["elements"][0].update(text="*Other* <@UOTHER>"),
            lambda m: m["blocks"][0].update(accessory={"type": "image"}),
            lambda m: nodes(m)[0].update(style={"bold": True}),
            lambda m: nodes(m)[0].update(text="AI Infra 晨报 · 2026-11-02 · Wrong date"),
            lambda m: nodes(m)[-1].update(text="click here"),
            lambda m: nodes(m)[-1].update(text=[]),
            lambda m: nodes(m)[-1].update(url="https://evil.example/2026-11-01/r1/"),
            lambda m: nodes(m)[-1].update(url=PROFILE["report_base_url"] + "2026-02-30/r1/"),
            lambda m: nodes(m)[-1].update(from_llm="true"),
            lambda m: m["files"][0].update(user="UOTHER"),
            lambda m: m["files"][0].update(user_team="TOTHER"),
            lambda m: m["files"][0].update(mode="external"),
            lambda m: m["files"][0].update(size=3000000),
            lambda m: m["files"][0].update(mimetype="image/svg+xml"),
            lambda m: m["files"][0].update(url_private="https://evil.example/image.png"),
            lambda m: m["files"].append(deepcopy(m["files"][0])),
            lambda m: m.update(files=[]),
        ]
        for index, mutate in enumerate(mutations):
            message = notice()
            mutate(message)
            with self.subTest(mutation=index), self.assertRaises(r.RelayError):
                self.parse(message)

    def test_invalid_profile_is_rejected(self):
        for changes in ({"user_id": "BOTHER"}, {"app_id": "wrong"}, {"not_before": "now"},
                        {"attribution_text": "unbound"}, {"report_base_url": "http://example.org/"},
                        {"report_base_url": "https://example.org/?secret=x"}, {"unknown": True}):
            with self.subTest(changes=changes), self.assertRaises(r.RelayError):
                r.validate_notice_connector({**PROFILE, **changes})

    def test_escaped_fallback_matches_visible_ampersand(self):
        message = notice()
        nodes = message["blocks"][0]["elements"][0]["elements"]
        nodes[0]["text"] = nodes[0]["text"].replace("and", "&")
        message["text"] = message["text"].replace("and", "&amp;")
        self.assertIn("Scheduling & memory", self.parse(message)["text"])


class NoticeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "state"
        root.mkdir(mode=0o700)
        self.store = r.Store(root)
        self.addCleanup(self.store.db.close)
        self.slack, self.wecom = FakeSlack(), FakeWeCom()
        self.images, self.downloads = [], []
        def download(plan):
            self.downloads.append(plan["file_id"])
            return PNG
        self.slack.download_image = download
        self.wecom.send_image = lambda data, mime: self.images.append((data, mime)) or "sent"
        self.now = 1000.0

    def receiver(self, cls=r.Relay):
        receiver = cls(self.store, self.slack, self.wecom, NOTICE_CONFIG,
                       clock=lambda: self.now, sleeper=lambda s: setattr(self, "now", self.now + s))
        self.store.set("cursor", ts(0))
        self.store.set("handoff_binding_v1", resume.binding(NOTICE_CONFIG))
        receiver.connected = True
        return receiver

    def exercise_poll(self, cls):
        receiver = self.receiver(cls)
        self.slack.messages = [notice(), notice(2, file_id="FTEST2"), notice(3, revision=2), brief(4)]
        for _ in range(6):
            receiver.poll()
        self.assertEqual(self.wecom.sent[:2], [receiver.delivery_text(notice()), receiver.delivery_text(notice(3, revision=2))])
        self.assertEqual(len(self.wecom.sent), 3)  # 原有机器人发送者仍可投递。
        self.assertEqual(len(self.images), 2)
        self.assertEqual(self.downloads, ["FTEST1", "FTEST1"])
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "already_sent")
        self.assertEqual(self.store.get("cursor"), ts(4))

    def test_direct_poll_deduplicates_reposted_edition(self):
        self.exercise_poll(r.Relay)

    def test_controlled_resume_poll_normalizes_and_deduplicates(self):
        self.exercise_poll(resume.resume_class(r))

    def test_unknown_delivery_is_never_retried_under_new_timestamp(self):
        receiver = self.receiver()
        self.wecom.results = ["unknown"]
        self.slack.messages = [notice(), notice(2, file_id="FTEST2")]
        receiver.poll()
        receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(self.images, [])
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "source_already_attempted")

    def test_profile_drift_or_removal_does_not_reset_existing_cursor(self):
        self.receiver()
        for config in (CONFIG, {**NOTICE_CONFIG, "notice_connector": {**PROFILE, "app_id": "AOTHER"}}):
            with self.subTest(config=config), self.assertRaisesRegex(r.RelayError, "notice_identity_changed"):
                r.Relay(self.store, self.slack, self.wecom, config)
        self.assertEqual(self.store.get("cursor"), ts(0))

    def test_untrusted_new_sender_is_not_accepted_as_legacy(self):
        receiver = self.receiver()
        self.slack.messages = [{**notice(), "app_id": "AOTHER"}]
        receiver.poll()
        self.assertEqual(self.wecom.sent, [])
        self.assertEqual(self.images, [])

    def test_edit_cannot_bypass_manual_timestamp_deduplication(self):
        receiver = self.receiver()
        self.slack.messages = [notice()]
        receiver.poll()
        self.slack.messages[0]["edited"] = {"ts": ts(2)}
        result = receiver.dispatch("/manual/slack-message", {"request_id": "edited-copy", "message_ts": ts(1)}, os.getuid())
        self.assertEqual(result["status"], "already_sent")
        self.assertEqual(len(self.wecom.sent), 1)
        self.assertEqual(len(self.images), 1)

    def test_legacy_timestamp_key_survives_edition_deduplication(self):
        receiver = self.receiver()
        self.slack.messages = [notice(), notice(2, file_id="FTEST2")]
        receiver.poll()
        for n, status in ((1, "sent"), (2, "already_sent")):
            legacy_key = r.digest([CONFIG["team_id"], CONFIG["channel_id"], ts(n)])
            row = self.store.db.execute("SELECT status FROM requests WHERE source_key=?", (legacy_key,)).fetchone()
            self.assertEqual(row, (status,))
        self.assertEqual(len(self.wecom.sent), 1)

    def test_config_requires_pinned_reader_and_images(self):
        root = Path(self.temp.name)
        for name in ("secrets", "run"):
            (root / name).mkdir(mode=0o700)
        path = root / "config.json"
        for config, valid in ((NOTICE_CONFIG, True), (CONFIG, True),
                              ({**NOTICE_CONFIG, "forward_images": False}, False),
                              ({**NOTICE_CONFIG, "user_id": "UOTHER"}, False),
                              ({**NOTICE_CONFIG, "sender_id": "UOWNER"}, False)):
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            if valid:
                self.assertEqual(r.load_config(root), config)
            else:
                with self.assertRaises(r.RelayError):
                    r.load_config(root)


if __name__ == "__main__":
    unittest.main()
