"""原始字节传输的离线测试；消息与图片元数据均为合成数据。"""
import base64
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import struct
import unittest
from unittest.mock import patch
import zlib

import notice_format as n
import notice_check as check
import relay as r
import resume
import test_article_notice as article_fixtures
from test_article_notice import message, COMMIT
from test_images import PNG, Response
from test_notice_connector import PROFILE, NOTICE_CONFIG
from test_notice_check import Slack, Reader
from test_relay import ts


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture():
    # 合成 caBX 数据只验证字节保留，不声明 C2PA 签名有效。
    kind, payload = b"caBX", b"synthetic provenance preservation fixture"
    chunk = struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    poster = PNG[:33] + chunk + PNG[33:]
    article = b"# Synthetic source\n"
    report = {"schema_version": "1.0", "date": "2026-11-01", "revision": 1,
              "title": "Synthetic report", "subtitle": "Offline", "entries": [],
              "articles": [{"id": "memory", "title": "Memory", "summary": "Not in notification", "sha256": sha(article)}],
              "poster": "assets/poster.png", "images": [{"path": "assets/poster.png", "sha256": sha(poster), "alt": "Synthetic"}]}
    metadata = json.dumps(report).encode()
    root = "reports/2026-11-01/r1/"
    value = {"schema_version": "1.0", "request_id": "isolated-bundle-fixture", "repository": "sfcloud-ai/ai-infra-reports",
             "expected_base_sha": "b" * 40, "prompt_commit_sha": "c" * 40,
             "files": [{"path": root + path, "sha256": sha(data), "base64": base64.b64encode(data).decode()}
                       for path, data in [("report.json", metadata), ("articles/memory.md", article), ("assets/poster.png", poster)]]}
    data = json.dumps(value).encode()
    manifest = n.build_manifest(report, PROFILE["report_base_url"], sha(metadata), COMMIT)
    m = message(edition=manifest)
    m["files"][0].update(name=n.BUNDLE_FILENAME, mimetype="text/plain", filetype="json", size=len(data))
    return data, poster, manifest, m


class BundleContractTests(unittest.TestCase):
    def test_integral_numeric_revision_matches_publisher_without_rewriting_metadata(self):
        data, poster, manifest, _ = fixture()
        for revision in (1, 1.0, True, 1.5, 9007199254740992, float("inf")):
            value = json.loads(data); item = value["files"][0]
            report = json.loads(base64.b64decode(item["base64"])); report["revision"] = revision
            metadata = json.dumps(report).encode(); item.update(base64=base64.b64encode(metadata).decode(), sha256=sha(metadata))
            expected = {**manifest, "report_sha256": sha(metadata)}
            if type(revision) is not bool and revision == 1:
                self.assertEqual(n.bundle_poster(json.dumps(value).encode(), expected, PROFILE["report_base_url"]), poster)
            else:
                with self.assertRaises(ValueError):
                    n.bundle_poster(json.dumps(value).encode(), expected, PROFILE["report_base_url"])

    def test_json_download_uses_pinned_original_url_exact_length_and_existing_scope(self):
        data, _, manifest, m = fixture()
        plan = r.connector_notice(m, PROFILE, "TTEST", "DFIXED", notice_manifest=manifest)["images"][0]
        slack = r.Slack("synthetic-in-memory", NOTICE_CONFIG)
        with self.assertRaisesRegex(r.ImageError, "scope"):
            slack.download_bundle(plan)
        slack.images_authorized = True
        calls = []
        class HTTP:
            def open(self, request, timeout):
                calls.append(request)
                return Response(data, headers={"Content-Type": "text/plain", "Content-Length": str(len(data))}, url=plan["url"])
        slack.http = HTTP()
        with patch.object(r, "image_download_timeout", nullcontext):
            self.assertEqual(slack.download_bundle(plan), data)
            with self.assertRaises(r.ImageError):
                slack.download_image(plan)
            with self.assertRaisesRegex(r.ImageError, "size"):
                slack.download_bundle({**plan, "size": len(data) - 1})
        self.assertEqual(calls[0].full_url, m["files"][0]["url_private"])
        self.assertEqual(calls[0].get_header("Accept-encoding"), "identity")

    def test_exact_original_bytes_including_provenance(self):
        data, poster, manifest, m = fixture()
        self.assertEqual(n.bundle_poster(data, manifest, PROFILE["report_base_url"]), poster)
        self.assertIn(b"caBX", poster)
        self.assertEqual(r.validate_image(poster, "image/png"), r.validate_image(PNG, "image/png"))
        self.assertNotEqual(sha(poster), sha(PNG))
        plan = r.connector_notice(m, PROFILE, "TTEST", "DFIXED", notice_manifest=manifest)["images"][0]
        self.assertEqual(r.original_bundle_poster(data, plan), poster)

    def test_bundle_cannot_change_source_metadata_paths_or_original_bytes(self):
        data, _, manifest, _ = fixture()
        for mutate in (
            lambda v: v.update(repository="other/repository"),
            lambda v: v.update(expected_base_sha="unknown"),
            lambda v: v["files"].append(deepcopy(v["files"][0])),
            lambda v: v["files"][1].update(path="reports/2026-11-02/r1/articles/memory.md"),
            lambda v: v["files"][1].update(base64=v["files"][1]["base64"] + "\n"),
            lambda v: v["files"][2].update(base64=base64.b64encode(PNG).decode(), sha256=sha(PNG)),
            lambda v: v["files"][0].update(base64=base64.b64encode(b"{}").decode(), sha256=sha(b"{}")),
            lambda v: v["files"].pop(1),
        ):
            value = json.loads(data); mutate(value)
            with self.assertRaises((ValueError, KeyError)):
                n.bundle_poster(json.dumps(value).encode(), manifest, PROFILE["report_base_url"])
        with self.assertRaises(ValueError):
            n.bundle_poster(data.replace(b'"schema_version": "1.0"', b'"schema_version": "1.0", "schema_version": "1.0"', 1), manifest, PROFILE["report_base_url"])
        changed = deepcopy(manifest); changed["poster"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            n.bundle_poster(data, changed, PROFILE["report_base_url"])

    def test_untrusted_json_metadata_is_rejected_before_download(self):
        _, _, manifest, original = fixture()
        for changes in ({"name": "other.json"}, {"mimetype": "image/png"}, {"filetype": "txt"}, {"mode": "snippet"},
                        {"file_access": "check_file_info"}, {"is_external": True}, {"size": n.MAX_BUNDLE_BYTES + 1},
                        {"user": "UOTHER"}, {"user_team": "TOTHER"}, {"url_private": "https://evil.example/data"}):
            m = deepcopy(original); m["files"][0].update(changes)
            with self.assertRaises(r.RelayError):
                r.connector_notice(m, PROFILE, "TTEST", "DFIXED", notice_manifest=manifest)


class BundleDeliveryTests(unittest.TestCase):
    setUp = article_fixtures.ArticleDeliveryTests.setUp
    receiver = article_fixtures.ArticleDeliveryTests.receiver
    def bundle_receiver(self):
        data, poster, manifest, m = fixture()
        self.slack.download_bundle = lambda plan: data
        self.slack.download_image = lambda plan: self.fail("JSON must never enter PNG download path")
        return self.receiver(expected=manifest), data, poster, manifest, m

    def test_bundle_delivers_only_expected_text_and_original_poster_once(self):
        receiver, data, poster, manifest, m = self.bundle_receiver()
        self.slack.messages = [m]; receiver.poll()
        self.assertEqual(self.wecom.sent, [manifest["text"]])
        self.assertEqual(self.images, [(poster, "image/png")])
        self.assertNotIn("Not in notification", self.wecom.sent[0])
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "sent")
        self.slack.messages.append({**deepcopy(m), "ts": ts(2)})
        restarted = self.receiver(expected=manifest); restarted.poll(); restarted.poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertEqual(len(self.images), 1)

    def test_bad_bundle_fails_before_any_wecom_text_or_image(self):
        receiver, _, _, _, m = self.bundle_receiver()
        self.slack.download_bundle = lambda plan: b"{}"
        self.slack.messages = [m]; receiver.poll()
        self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
        result = self.store.status("auto:" + ts(1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["images"][0]["error"], "notice_bundle_invalid")

    def test_bundle_read_outage_retains_cursor_and_recovers_without_duplicate(self):
        receiver, data, poster, _, m = self.bundle_receiver()
        self.slack.messages = [m]
        with patch.object(self.slack, "download_bundle", side_effect=r.ImageError("image_download_incomplete")):
            with self.assertRaises(resume.RetryableReadError):
                receiver.poll()
        self.assertEqual(self.store.get("cursor"), ts(0)); self.assertFalse(self.wecom.sent)
        self.assertIsNone(self.store.status("auto:" + ts(1)))
        receiver.poll(); receiver.poll()
        self.assertEqual(self.images, [(poster, "image/png")])

    def test_unknown_bundle_delivery_is_not_repeated_after_restart(self):
        receiver, _, _, manifest, m = self.bundle_receiver()
        self.wecom.results = ["unknown"]; self.slack.messages = [m]; receiver.poll()
        self.slack.messages.append({**deepcopy(m), "ts": ts(2)})
        self.receiver(expected=manifest).poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertFalse(self.images)


class BundleCheckerTests(unittest.TestCase):
    def setUp(self):
        self.data, self.poster, self.manifest, m = fixture()
        self.slack = Slack(); self.slack.message = m
        self.slack.download_bundle = lambda plan: self.data
        self.request = {"message_ts": m["ts"], "file_id": m["files"][0]["id"], "notice_manifest": self.manifest,
                        "transport": "report_bundle_v1", "bundle_sha256": sha(self.data), "bundle_bytes": len(self.data)}

    def run_check(self):
        return check.read_received_notice(json.dumps(self.request).encode(), self.slack.config, self.slack, notice_reader=Reader(self.manifest))

    def test_independent_receipt_proves_both_transport_and_original(self):
        result = self.run_check(); receipt = result["receipt"]
        self.assertEqual((receipt["transport_sha256"], receipt["transport_bytes"]), (sha(self.data), len(self.data)))
        self.assertEqual((receipt["poster_sha256"], receipt["poster_bytes"]), (sha(self.poster), len(self.poster)))
        self.assertEqual(result["messages_sent"], 0); self.assertFalse(result["state_changed"])

    def test_same_poster_in_rewritten_bundle_does_not_match_reserved_raw_bytes(self):
        self.data = json.dumps(json.loads(self.data), indent=2).encode()
        with self.assertRaisesRegex(check.NoticeCheckError, "bundle differs"):
            self.run_check()

    def test_legacy_request_cannot_certify_bundle_and_source_mismatch_fails(self):
        for key in ("transport", "bundle_sha256", "bundle_bytes"):
            del self.request[key]
        with self.assertRaisesRegex(check.NoticeCheckError, "transport differs"):
            self.run_check()

    def test_changed_message_after_bundle_read_is_rejected(self):
        def read(plan):
            self.slack.message["edited"] = {}; return self.data
        self.slack.download_bundle = read
        with self.assertRaisesRegex(check.NoticeCheckError, "changed during"):
            self.run_check()
