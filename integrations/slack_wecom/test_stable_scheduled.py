"""稳定网页与历史投递身份的离线兼容；不读取生产账本或调用真实传输。"""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest

import notice_format as n
import relay
import scheduled_runtime
from scheduled_delivery import ScheduledDeliveryError, scheduled_notice_day
from test_notice_bundle import fixture
from test_notice_connector import PROFILE
import test_scheduled_inspect as inspection_fixtures


BASE = PROFILE["report_base_url"]


def package(revision=1, *, stable=True, stable_source=True):
    data, poster, _, _ = fixture()
    value = json.loads(data)
    metadata = value["files"][0]
    report = json.loads(base64.b64decode(metadata["base64"]))
    report["revision"] = revision
    raw = json.dumps(report).encode()
    metadata.update(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    root = "reports/2026-11-01/" + ("" if stable_source else f"r{revision}/")
    for item in value["files"]:
        item["path"] = root + item["path"].split("/r1/", 1)[1]
    return json.dumps(value).encode(), poster, n.build_manifest(report, BASE, metadata["sha256"], "a" * 40, stable=stable)


class StableFormatTests(unittest.TestCase):
    def test_exact_stable_and_legacy_manifests_bind_original_bytes(self):
        for revision in (1, 2):
            for stable, stable_source in ((False, False), (True, False), (True, True)):
                with self.subTest(revision=revision, stable=stable, stable_source=stable_source):
                    data, poster, manifest = package(revision, stable=stable, stable_source=stable_source)
                    original = bytes(data)
                    self.assertEqual(n.validate_manifest(manifest, BASE), manifest)
                    self.assertEqual(n.bundle_poster(data, manifest, BASE), poster)
                    self.assertEqual(data, original)
                    root = BASE + "2026-11-01/" + ("" if stable else f"r{revision}/")
                    self.assertEqual(manifest["index_url"], root)
                    self.assertEqual(manifest["articles"][0]["url"], root + "memory/")

    def test_mixed_public_urls_and_source_roots_fail_closed(self):
        data, _, manifest = package()
        for mutate in (
            lambda m: m.update(index_url=BASE + "2026-11-01/r1/"),
            lambda m: m["articles"][0].update(url=BASE + "2026-11-01/r1/memory/"),
            lambda m: m["poster"].update(url=BASE + "reports/2026-11-01/r1/assets/poster.png"),
            lambda m: m.update(text=m["text"].replace("/memory/", "/r1/memory/")),
            lambda m: m.update(revision=True),
        ):
            changed = copy.deepcopy(manifest); mutate(changed)
            with self.assertRaises(ValueError): n.validate_manifest(changed, BASE)
        for mutate in (
            lambda v: v["files"][1].update(path="reports/2026-11-01/r1/articles/memory.md"),
            lambda v: v["files"][0].update(path="reports/2026-11-02/report.json"),
            lambda v: v["files"].append({**v["files"][0], "path": "reports/2026-11-01/r1/report.json"}),
        ):
            changed = json.loads(data); mutate(changed)
            with self.assertRaises(ValueError): n.bundle_poster(json.dumps(changed).encode(), manifest, BASE)
        _, _, legacy = package(stable=False, stable_source=False)
        with self.assertRaises(ValueError): n.bundle_poster(data, legacy, BASE)

    def test_current_key_requires_explicit_delivery_context_and_remains_strict(self):
        data, _, manifest = package()
        self.assertEqual(scheduled_notice_day(manifest["index_url"], PROFILE), "2026-11-01")
        with self.assertRaises(relay.RelayError): relay.notice_url_day(manifest["index_url"], PROFILE)
        self.assertEqual(relay.notice_url_day(manifest["index_url"], PROFILE, allow_current=True), "2026-11-01")
        self.assertEqual(n.parse_article_text(manifest["text"], BASE)['index_url'], manifest['index_url'])
        for suffix in ("2026-11-01", "2026-02-30/", "2026-11-01/?r=1", "2026-11-01/#x", "2026-11-01/r0/", "2026-11-01/memory/", "2026-11-01//"):
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, relay.RelayError)): scheduled_notice_day(BASE + suffix, PROFILE)
        for revision in (1, 2):
            self.assertEqual(scheduled_notice_day(BASE + f"2026-11-01/r{revision}/", PROFILE), "2026-11-01")


class StableScheduledTests(unittest.TestCase):
    setUp = inspection_fixtures.InspectionTests.setUp
    tearDown = inspection_fixtures.InspectionTests.tearDown
    queue = inspection_fixtures.InspectionTests.queue
    send = inspection_fixtures.InspectionTests.send
    admit = inspection_fixtures.InspectionTests.admit
    advance = inspection_fixtures.InspectionTests.advance
    inspect = inspection_fixtures.InspectionTests.inspect
    assert_read_only = inspection_fixtures.InspectionTests.assert_read_only

    def configure(self, revision=1, *, stable=True, stable_source=True):
        self.bundle, self.poster, manifest = package(revision, stable=stable, stable_source=stable_source)
        self.plan.update(bundle_sha256=hashlib.sha256(self.bundle).hexdigest(), notice_key=manifest["index_url"],
                         text=manifest["text"], poster=self.poster, poster_sha256=hashlib.sha256(self.poster).hexdigest())

    def test_stable_admission_readonly_inspection_restart_and_single_delivery(self):
        self.configure(); self.admit(); self.admit()
        original = self.store.db.execute("SELECT record,bundle,poster FROM scheduled_notices").fetchone()
        self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["status"], "queued")
        self.q = self.queue(); self.q.tick(); self.advance(); self.advance()
        self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["phase"], "complete")
        self.assertEqual(self.calls, [("text", self.plan["text"]), ("image", self.poster)])
        self.assertEqual(self.store.db.execute("SELECT record,bundle,poster FROM scheduled_notices").fetchone(), original)

    def test_original_r1_r2_intents_keep_exact_keys_bytes_and_fail_on_reinterpretation(self):
        for revision in (1, 2):
            with self.subTest(revision=revision):
                # 每轮使用独立合成账本，不迁移任何生产记录。
                if revision == 2:
                    self.tearDown(); self.setUp()
                self.configure(revision, stable=False, stable_source=False); self.admit()
                self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["status"], "queued")
                before = list(self.store.db.iterdump())
                self.configure(revision, stable=True, stable_source=False)
                with self.assertRaises(ScheduledDeliveryError): self.admit()
                self.assertEqual(list(self.store.db.iterdump()), before)
                self.q.tick()
                self.assertEqual(self.calls, [])
                self.assertEqual(self.q.status("isolated-bundle-fixture")["last_error"], "publication_mismatch")
                self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["status"], "queued")

    def test_stable_manifest_with_original_versioned_bundle_is_inspectable(self):
        self.configure(2, stable=True, stable_source=False); self.admit()
        self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["status"], "queued")
        self.assertIn(b"reports/2026-11-01/r2/", self.bundle)

    def test_stable_unknown_is_frozen_and_future_day_can_be_inspected(self):
        self.configure(); self.admit(); self.outcomes = [TimeoutError()]; self.q.tick()
        original = list(self.store.db.iterdump())
        self.assertEqual(self.assert_read_only(self.inspect)["scheduler"]["phase"], "unknown")
        scheduled_runtime.ScheduledRuntime.assert_reconciled(SimpleNamespace(queue=self.q))
        self.assertIsNone(self.assert_read_only(lambda: self.inspect("next-day-fixture", "2026-11-02"))["scheduler"])
        self.q = self.queue(); self.assertIsNone(self.advance(99999))
        self.assertEqual(list(self.store.db.iterdump()), original)
        self.assertEqual(len(self.calls), 1)

    def test_sent_legacy_day_blocks_new_stable_request_without_rewriting_history(self):
        self.configure(2, stable=False, stable_source=False); self.admit(); self.q.tick(); self.advance()
        before = list(self.store.db.iterdump())
        self.configure()
        value = json.loads(self.bundle); value["request_id"] = "stable-second-request"
        self.bundle = json.dumps(value).encode()
        self.plan.update(report_request_id=value["request_id"], bundle_sha256=hashlib.sha256(self.bundle).hexdigest())
        with self.assertRaises(ScheduledDeliveryError): self.q.admit(value["request_id"], self.bundle, "a" * 40)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
