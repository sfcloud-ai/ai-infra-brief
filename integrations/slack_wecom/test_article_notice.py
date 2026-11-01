"""分项通知离线回归；所有身份、图片与传输均为合成数据。"""
from copy import deepcopy
import hashlib
import signal
import struct
import zlib
from email.message import Message
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import relay as r
import notice_format as n
import resume
import runtime
from test_images import PNG, attachment, Response
import test_images as image_fixtures
from test_notice_connector import PROFILE, NOTICE_CONFIG, notice as old_notice
from test_relay import CONFIG, FakeSlack, FakeWeCom, ts

COMMIT = "a" * 40


def report():
    return {"date": "2026-11-01", "revision": 1,
            "articles": [{"id": "memory", "title": "显存预算与缓存", "summary": "不应出现在通知里的摘要"},
                         {"id": "service", "title": "服务边界 & 升级", "summary": "第二段摘要"}],
            "poster": "assets/overview.png", "images": [{"path": "assets/overview.png", "sha256": hashlib.sha256(PNG).hexdigest()}]}


def metadata_png():
    # 只插入合法 tEXt 块；IHDR 与压缩像素数据逐字节保持不变。
    kind, data = b"tEXt", b"Comment\0transport metadata"
    chunk = struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return PNG[:33] + chunk + PNG[33:]


def manifest():
    return n.build_manifest(report(), PROFILE["report_base_url"], "b" * 64, COMMIT)


def message(index=1, edition=None):
    edition = edition or manifest()
    nodes = [{"type": "text", "text": "AI Infra 晨报 · " + edition["date"]}]
    for article in edition["articles"]:
        nodes.extend([{"type": "text", "text": "\n\n" + article["title"] + "\n"},
                      {"type": "link", "url": article["url"], "text": article["url"].removeprefix("https://").rstrip("/"), "from_llm": True}])
    return {"type": "message", "user": PROFILE["user_id"], "app_id": PROFILE["app_id"], "ts": ts(index),
            "text": edition["text"] + " " + PROFILE["attribution_text"],
            "blocks": [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": nodes}]},
                       {"type": "context", "elements": [{"type": "mrkdwn", "text": PROFILE["attribution_text"], "verbatim": False}]}],
            "files": [attachment(user=PROFILE["user_id"])]}


class ArticleContractTests(unittest.TestCase):
    def parse(self, m, expected=None):
        return r.connector_notice(m, PROFILE, CONFIG["team_id"], CONFIG["channel_id"], notice_manifest=expected or manifest())

    def test_all_subtitles_and_urls_without_summary_or_topic_only_heading(self):
        m = manifest()
        self.assertEqual(m["text"], "AI Infra 晨报 · 2026-11-01\n\n显存预算与缓存\n"
                         + PROFILE["report_base_url"] + "2026-11-01/r1/memory/\n\n服务边界 & 升级\n"
                         + PROFILE["report_base_url"] + "2026-11-01/r1/service/")
        parsed = self.parse(message())
        self.assertEqual(parsed["text"], m["text"])
        self.assertEqual(parsed["url"], m["index_url"])
        self.assertEqual(parsed["images"][0]["expected_sha256"], hashlib.sha256(PNG).hexdigest())

    def test_manifest_is_required_not_just_same_origin_regex(self):
        with self.assertRaisesRegex(r.RelayError, "notice_manifest_rejected"):
            r.connector_notice(message(), PROFILE, CONFIG["team_id"], CONFIG["channel_id"])

    def test_incomplete_reordered_or_changed_metadata_is_rejected(self):
        for mutate in (lambda x: x["articles"].pop(), lambda x: x["articles"].reverse(),
                       lambda x: x["articles"][0].update(title="被替换的标题")):
            data = report(); mutate(data)
            altered = n.build_manifest(data, PROFILE["report_base_url"], "b" * 64, COMMIT)
            with self.assertRaises(r.RelayError):
                self.parse(message(edition=altered))

    def test_unsupported_visible_structure_and_fallback_divergence_rejected(self):
        mutations = [lambda x: x.update(text="forged fallback"),
                     lambda x: x["blocks"][0]["elements"][0]["elements"][1].update(style={"bold": True}),
                     lambda x: x["blocks"][0]["elements"][0]["elements"][2].update(url="https://evil.example/path"),
                     lambda x: x["blocks"][0]["elements"][0]["elements"][2].update(text="click here"),
                     lambda x: x["blocks"][0]["elements"][0]["elements"].append({"type": "user", "user_id": "UOTHER"}),
                     lambda x: x.update(app_id="AOTHER"), lambda x: x.update(edited={"ts": ts(2)})]
        for mutate in mutations:
            m = message(); mutate(m)
            with self.assertRaises(r.RelayError):
                self.parse(m)

    def test_standard_link_fallback_and_entity_escaping(self):
        m = message()
        for node in m["blocks"][0]["elements"][0]["elements"]:
            if node["type"] == "link":
                m["text"] = m["text"].replace(node["url"], "<" + node["url"] + "|" + node["text"] + ">")
        m["text"] = m["text"].replace(" & ", " &amp; ")
        self.assertEqual(self.parse(m)["text"], manifest()["text"])

    def test_manifest_schema_origin_and_poster_binding(self):
        mutations = [lambda x: x.update(commit="local-preview"), lambda x: x.update(extra=True),
                     lambda x: x["articles"][0].update(url=x["articles"][0]["url"] + "?q=x"),
                     lambda x: x["articles"][1].update(url=x["articles"][1]["url"].replace("r1/", "r2/")),
                     lambda x: x["poster"].update(url="https://evil.example/poster.png"),
                     lambda x: x.update(report_sha256="bad"), lambda x: x.update(text=x["text"] + "\nextra")]
        for mutate in mutations:
            value = manifest(); mutate(value)
            with self.assertRaises((ValueError, r.RelayError)):
                self.parse(message(), value)

    def test_byte_budget_uses_utf8_and_never_omits_articles(self):
        data = report()
        data["articles"] = [{"id": "item-" + str(i), "title": "字" * 90} for i in range(8)]
        with self.assertRaises(ValueError):
            n.build_manifest(data, PROFILE["report_base_url"], "b" * 64, COMMIT)
        for bad in ("多行\n标题", "分隔\u2028符", "重复\u2029符", "<mention>"):
            data = report(); data["articles"][0]["title"] = bad
            with self.assertRaises(ValueError):
                n.build_manifest(data, PROFILE["report_base_url"], "b" * 64, COMMIT)


class ManifestReadTests(unittest.TestCase):
    def test_only_fixed_public_paths_and_stable_commit(self):
        calls = []
        publication = {"schema_version": "1.0", "commit": COMMIT, "preview": False, "reports": ["2026-11-01/r1"]}
        def read(url, limit):
            calls.append((url, limit))
            return deepcopy(publication if url.endswith("publication.json") else manifest())
        reader = n.NoticeManifestReader(PROFILE["report_base_url"], read)
        candidate = r._connector_candidate(message(), PROFILE, CONFIG["team_id"], CONFIG["channel_id"])
        self.assertEqual(reader.read(candidate), manifest())
        self.assertEqual([url for url, _ in calls], [PROFILE["report_base_url"] + "publication.json",
                         PROFILE["report_base_url"] + "reports/2026-11-01/r1/notice.json",
                         PROFILE["report_base_url"] + "publication.json"])

    def test_changed_deployment_or_read_failure_has_no_verified_result(self):
        candidate = r._connector_candidate(message(), PROFILE, CONFIG["team_id"], CONFIG["channel_id"])
        calls = []
        def read(url, limit):
            calls.append(url)
            if url.endswith("notice.json"):
                return manifest()
            return {"schema_version": "1.0", "commit": COMMIT if len(calls) == 1 else "c" * 40,
                    "preview": False, "reports": ["2026-11-01/r1"]}
        with self.assertRaises(n.NoticeReadError):
            n.NoticeManifestReader(PROFILE["report_base_url"], read).read(candidate)


    def test_public_reads_use_no_auth_and_reject_wrong_content_type_or_size(self):
        reader = n.NoticeManifestReader(PROFILE["report_base_url"])
        requests = []
        class Response:
            status = 200
            data = b'{"ok":true}'
            def __init__(self, content_type):
                self.headers = Message(); self.headers["Content-Type"] = content_type
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, count): return self.data[:count]
        class HTTP:
            response = Response("application/json")
            def open(self, request, timeout):
                requests.append(request); return self.response
        http = HTTP(); reader.http = http
        self.assertEqual(reader._read_json(PROFILE["report_base_url"] + "publication.json", 100), {"ok": True})
        self.assertIsNone(requests[0].get_header("Authorization"))
        self.assertIsNone(requests[0].get_header("Cookie"))
        for response, limit in ((Response("text/html"), 100), (Response("application/json"), 2)):
            http.response = response
            with self.assertRaises(n.NoticeReadError):
                reader._read_json(PROFILE["report_base_url"] + "publication.json", limit)

    @unittest.skipUnless(hasattr(signal, "setitimer"), "生产服务要求 Linux 总超时能力")
    def test_total_read_deadline_restores_signal_and_rejects_existing_alarm(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(n.NoticeReadError):
            with n.public_read_deadline():
                signal.raise_signal(signal.SIGALRM)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        signal.setitimer(signal.ITIMER_REAL, 60)
        try:
            with self.assertRaises(n.NoticeReadError):
                with n.public_read_deadline():
                    self.fail("已有期限不能被覆盖")
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_same_origin_unlisted_article_is_not_trusted(self):
        original = manifest()
        changed = report(); changed["articles"][0]["id"] = "made-up"
        candidate_manifest = n.build_manifest(changed, PROFILE["report_base_url"], "b" * 64, COMMIT)
        candidate = r._connector_candidate(message(edition=candidate_manifest), PROFILE, CONFIG["team_id"], CONFIG["channel_id"])
        publication = {"schema_version": "1.0", "commit": COMMIT, "preview": False, "reports": ["2026-11-01/r1"]}
        reader = n.NoticeManifestReader(PROFILE["report_base_url"], lambda url, limit: publication if url.endswith("publication.json") else original)
        trusted = reader.read(candidate)
        with self.assertRaisesRegex(r.RelayError, "notice_manifest_rejected"):
            r._bind_notice_manifest(candidate, trusted, PROFILE)


class ArticleDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name) / "state"; state.mkdir(mode=0o700)
        self.store = r.Store(state); self.addCleanup(self.store.db.close)
        self.slack, self.wecom, self.images = FakeSlack(), FakeWeCom(), []
        self.slack.download_image = lambda plan: PNG
        self.wecom.send_image = lambda data, mime: self.images.append((data, mime)) or "sent"
        self.now = 1000.0

    def receiver(self, *, resume_mode=True, fail_first=False, expected=None):
        expected = expected or manifest()
        owner = self
        class Reader:
            calls = 0
            def read(self, candidate):
                self.calls += 1
                if fail_first and self.calls == 1:
                    raise n.NoticeReadError("synthetic_read_failure")
                return deepcopy(expected)
        self.reader = Reader()
        cls = resume.resume_class(r) if resume_mode else r.Relay
        receiver = cls(self.store, self.slack, self.wecom, NOTICE_CONFIG,
                       clock=lambda: owner.now, sleeper=lambda seconds: setattr(owner, "now", owner.now + seconds),
                       notice_reader=self.reader)
        self.store.set("cursor", ts(0)); self.store.set("handoff_binding_v1", resume.binding(NOTICE_CONFIG))
        receiver.connected = True
        return receiver

    def test_read_outage_keeps_cursor_and_pending_message_then_sends_once(self):
        receiver = self.receiver(fail_first=True); self.slack.messages = [message()]
        with self.assertRaises(resume.RetryableReadError):
            receiver.poll()
        self.assertEqual(self.store.get("cursor"), ts(0)); self.assertFalse(self.wecom.sent)
        self.assertIsNone(self.store.status("auto:" + ts(1)))
        receiver.poll(); receiver.poll()
        self.assertEqual(self.reader.calls, 2)
        self.assertEqual(self.wecom.sent, [manifest()["text"]]); self.assertEqual(len(self.images), 1)
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "sent")

    def test_controlled_read_outage_stays_active_then_recovers_without_reactivation(self):
        receiver = self.receiver(fail_first=True); self.slack.messages = [message()]
        controller = runtime.ControlledRelay(r, receiver, COMMIT)
        controller.phase = "active"
        with self.assertRaises(resume.RetryableReadError):
            controller.poll()
        self.assertEqual(controller.phase, "active")
        self.assertEqual(self.store.get("cursor"), ts(0))
        self.assertFalse(self.wecom.sent)
        self.assertEqual(controller.polls, 0)
        controller.poll()
        self.assertEqual(controller.phase, "active")
        self.assertEqual(controller.polls, 1)
        self.assertEqual(self.wecom.sent, [manifest()["text"]])
        self.assertEqual(len(self.images), 1)

    def test_direct_reader_outage_does_not_trigger_skip_to_latest_baseline(self):
        receiver = self.receiver(resume_mode=False, fail_first=True); self.slack.messages = [message()]
        with self.assertRaises(n.NoticeReadError):
            receiver.poll()
        self.assertTrue(receiver.connected); self.assertEqual(self.store.get("cursor"), ts(0))
        receiver.poll(); self.assertEqual(len(self.wecom.sent), 1)

    def test_old_and_new_format_keep_same_edition_dedupe_key(self):
        receiver = self.receiver(); self.slack.messages = [old_notice(1), message(2)]
        receiver.poll(); receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertEqual(len(self.images), 1)
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "already_sent")

    def test_unknown_send_is_not_repeated_by_new_timestamp(self):
        receiver = self.receiver(); self.wecom.results = ["unknown"]
        self.slack.messages = [message(1), message(2)]
        receiver.poll(); receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertFalse(self.images)
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "source_already_attempted")

    def test_untrusted_sender_does_not_read_publication(self):
        receiver = self.receiver(); m = message(); m["app_id"] = "AOTHER"; self.slack.messages = [m]
        receiver.poll(); self.assertEqual(self.reader.calls, 0); self.assertFalse(self.wecom.sent)

    def test_prechecked_cache_is_not_reused_by_later_poll_attempt(self):
        for resume_mode in (False, True):
            with self.subTest(resume_mode=resume_mode):
                receiver = self.receiver(resume_mode=resume_mode)
                m = message(); receiver.notice(m)
                self.assertEqual(self.reader.calls, 1)
                def unavailable(candidate):
                    raise n.NoticeReadError("now_unavailable")
                self.reader.read = unavailable
                self.slack.messages = [m]
                with self.assertRaises((n.NoticeReadError, resume.RetryableReadError)):
                    receiver.poll()
                self.assertEqual(self.store.get("cursor"), ts(0)); self.assertFalse(self.wecom.sent)

    def test_manual_attempt_does_not_reuse_prechecked_cache(self):
        receiver = self.receiver(); m = message(); self.slack.messages = [m]
        receiver.notice(m)
        def unavailable(candidate):
            raise n.NoticeReadError("now_unavailable")
        self.reader.read = unavailable
        with self.assertRaises(n.NoticeReadError):
            receiver.dispatch("/manual/slack-message", {"request_id": "manual-fresh-check", "message_ts": ts(1)}, os.getuid())
        self.assertFalse(self.wecom.sent)

    def test_poster_content_must_match_the_published_manifest(self):
        expected = manifest(); expected["poster"]["sha256"] = "f" * 64
        receiver = self.receiver(expected=expected); self.slack.messages = [message()]
        receiver.poll(); self.assertFalse(self.images)
        result = self.store.status("auto:" + ts(1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["parts_sent"], 0)
        self.assertFalse(self.wecom.sent)
        self.assertEqual(result["images"][0]["error"], "notice_poster_hash_mismatch")

    def test_metadata_only_change_is_rejected_before_any_text_or_image_write(self):
        altered = metadata_png()
        self.assertEqual(r.validate_image(altered, "image/png"), r.validate_image(PNG, "image/png"))
        receiver = self.receiver(); m = message(); m["files"][0]["size"] = len(altered)
        self.slack.download_image = lambda plan: altered
        self.slack.messages = [m, message(2)]
        receiver.poll(); receiver.poll()
        self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "failed")
        self.assertEqual(self.store.status("auto:" + ts(1))["images"][0]["error"], "notice_poster_hash_mismatch")
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "source_already_attempted")

    def test_poster_is_downloaded_once_before_text_and_same_bytes_are_sent(self):
        events = []
        receiver = self.receiver(); self.slack.messages = [message()]
        def download(plan):
            self.assertEqual(events, [])
            events.append("download")
            return PNG
        def text(body):
            events.append("text")
            return "sent"
        def image(data, mime):
            self.assertIs(data, PNG)
            events.append("image")
            return "sent"
        self.slack.download_image = download; self.wecom.send = text; self.wecom.send_image = image
        receiver.poll()
        self.assertEqual(events, ["download", "text", "image"])
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "sent")

    def test_image_send_failure_after_preflight_is_partial_and_never_repeated(self):
        receiver = self.receiver(); self.slack.messages = [message(), message(2)]
        self.wecom.send_image = lambda data, mime: self.images.append(data) or "failed"
        receiver.poll(); receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertEqual(self.images, [PNG])
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "partial")
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "source_already_attempted")

    def test_unknown_image_result_after_preflight_is_not_repeated(self):
        receiver = self.receiver(); self.slack.messages = [message(), message(2)]
        self.wecom.send_image = lambda data, mime: self.images.append(data) or "unknown"
        receiver.poll(); receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1); self.assertEqual(self.images, [PNG])
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "unknown")
        self.assertEqual(self.store.status("auto:" + ts(2))["status"], "source_already_attempted")

    def test_download_failure_before_text_cannot_leave_partial_output(self):
        receiver = self.receiver(); self.slack.messages = [message()]
        def unavailable(plan):
            raise r.RelayError("synthetic_download_failure")
        self.slack.download_image = unavailable
        with self.assertRaises(resume.RetryableReadError):
            receiver.poll()
        self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
        self.assertIsNone(self.store.status("auto:" + ts(1)))
        self.assertEqual(self.store.get("cursor"), ts(0))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'notice_edition:%'").fetchone()[0], 0)
        self.slack.download_image = lambda plan: PNG
        receiver.poll(); receiver.poll()
        self.assertEqual(self.wecom.sent, [manifest()["text"]]); self.assertEqual(len(self.images), 1)
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "sent")

    def test_partial_http_body_with_valid_length_retries_same_message_then_sends_once(self):
        receiver = self.receiver(); self.slack.messages = [message()]
        transport = image_fixtures.ImageTransportTests().slack()
        self.slack.download_image = transport.download_image
        short = Response(PNG[:-4], headers={"Content-Type": "image/png", "Content-Length": str(len(PNG))})
        with patch.object(transport.http, "open", side_effect=[short, Response()]) as network:
            with self.assertRaises(resume.RetryableReadError):
                receiver.poll()
            self.assertIsNone(self.store.status("auto:" + ts(1)))
            self.assertEqual(self.store.get("cursor"), ts(0))
            self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
            receiver.poll(); receiver.poll()
            self.assertEqual(network.call_count, 2)
        self.assertEqual(self.wecom.sent, [manifest()["text"]]); self.assertEqual(len(self.images), 1)
        self.assertEqual(self.store.status("auto:" + ts(1))["status"], "sent")

    def test_declared_header_size_mismatch_remains_permanent_before_any_write(self):
        receiver = self.receiver(); self.slack.messages = [message()]
        transport = image_fixtures.ImageTransportTests().slack()
        self.slack.download_image = transport.download_image
        wrong = Response(PNG, headers={"Content-Type": "image/png", "Content-Length": str(len(PNG) + 1)})
        with patch.object(transport.http, "open", return_value=wrong):
            receiver.poll()
        self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
        result = self.store.status("auto:" + ts(1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["images"][0]["error"], "image_size_mismatch")

    def test_preflight_timeout_then_restart_preserves_same_message_for_one_send(self):
        receiver = self.receiver(); self.slack.messages = [message()]
        def timeout(plan):
            raise r.ImageError("image_download_timed_out")
        self.slack.download_image = timeout
        with self.assertRaises(resume.RetryableReadError):
            receiver.poll()
        self.store.db.close()
        self.store = r.Store(Path(self.temp.name) / "state"); self.addCleanup(self.store.db.close)
        self.assertEqual(self.store.get("cursor"), ts(0)); self.assertIsNone(self.store.status("auto:" + ts(1)))
        self.slack.download_image = lambda plan: PNG
        restarted = resume.resume_class(r)(self.store, self.slack, self.wecom, NOTICE_CONFIG,
                                          clock=lambda: self.now, sleeper=lambda seconds: setattr(self, "now", self.now + seconds),
                                          notice_reader=SimpleNamespace(read=lambda candidate: manifest()))
        restarted.poll(); restarted.poll()
        self.assertEqual(self.store.get("cursor"), ts(1))
        self.assertEqual(self.wecom.sent, [manifest()["text"]]); self.assertEqual(len(self.images), 1)

    def test_exit_during_preflight_rolls_back_admission_and_keeps_cursor(self):
        receiver = self.receiver(); self.slack.messages = [message()]
        def interrupted(plan):
            raise SystemExit("synthetic_exit")
        self.slack.download_image = interrupted
        with self.assertRaises(SystemExit):
            receiver.poll()
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.get("cursor"), ts(0))
        self.assertIsNone(self.store.status("auto:" + ts(1)))
        self.assertFalse(self.wecom.sent)
        self.slack.download_image = lambda plan: PNG
        receiver.poll()
        self.assertEqual(len(self.wecom.sent), 1)

    def test_competing_preflights_serialize_same_edition_before_external_writes(self):
        self.competing_preflights(fail_first=False)

    def test_competing_reader_recovers_after_other_preflight_rolls_back(self):
        self.competing_preflights(fail_first=True)

    def competing_preflights(self, *, fail_first):
        self.receiver()
        ready = threading.Barrier(3)
        reading, release = threading.Event(), threading.Event()
        reads, results, errors = [], [], []
        plans = r.connector_notice(message(), PROFILE, CONFIG["team_id"], CONFIG["channel_id"], notice_manifest=manifest())["images"]
        def download(plan):
            reads.append(plan["file_id"]); reading.set()
            if not release.wait(5):
                raise AssertionError("synthetic_test_deadline")
            if fail_first and len(reads) == 1:
                raise r.ImageError("image_download_timed_out")
            return PNG
        self.slack.download_image = download
        def worker(index):
            store = None
            try:
                store = r.Store(Path(self.temp.name) / "state")
                receiver = r.Relay(store, self.slack, self.wecom, NOTICE_CONFIG,
                                   clock=lambda: self.now, sleeper=lambda seconds: setattr(self, "now", self.now + seconds))
                ready.wait(5)
                result = receiver.deliver("auto:" + ts(index), {"mode": "auto", "ts": ts(index)}, manifest()["text"],
                                          ts(index), "auto", os.getuid(), image_plans=plans, notice_key=manifest()["index_url"])
                results.append(result["status"])
            except n.NoticeReadError:
                results.append("retryable_read")
            except BaseException as error:
                errors.append(error)
            finally:
                if store is not None:
                    store.db.close()
        workers = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
        for worker_thread in workers:
            worker_thread.start()
        try:
            ready.wait(5); self.assertTrue(reading.wait(5))
            self.assertFalse(self.wecom.sent); self.assertFalse(self.images)
        finally:
            release.set()
            for worker_thread in workers:
                worker_thread.join(5)
        self.assertFalse(errors, errors)
        self.assertFalse(any(worker_thread.is_alive() for worker_thread in workers))
        self.assertEqual(results.count("sent"), 1)
        self.assertTrue(set(results) <= {"sent", "already_sent", "source_already_attempted", "retryable_read"})
        self.assertEqual(results.count("retryable_read"), int(fail_first))
        self.assertEqual(len(reads), 2 if fail_first else 1)
        self.assertEqual(self.wecom.sent, [manifest()["text"]]); self.assertEqual(len(self.images), 1)


if __name__ == "__main__":
    unittest.main()
