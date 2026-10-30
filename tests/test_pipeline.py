from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from ai_brief.cli import main
from ai_brief.contracts import RESOURCE_ROOT, validate
from ai_brief.pipeline import canonical_url, implementation_fingerprint, run, simulate_delivery
from ai_brief.storage import BriefError, lock, read_json, write_json


AS_OF = datetime(2026, 10, 30, 8, tzinfo=timezone(timedelta(hours=8)))


def item(key, age=1, topic="model_serving", **overrides):
    """明确标注的合成测试数据；永远不请求 example.invalid。"""
    result = {
        "event_key": key, "title": "SYNTHETIC TEST ONLY " + key, "topic": topic,
        "published_at": (AS_OF - timedelta(hours=age)).isoformat(),
        "retrieved_at": AS_OF.isoformat(), "source_url": "https://example.invalid/" + key,
        "source_name": "SYNTHETIC TEST SOURCE", "source_type": "primary", "verification": "mock_unverified",
        "evidence": "Synthetic assertion for program testing, not news.", "summary": "Not a real event.",
        "relevance": "Exercises selection logic only.", "limitations": "Not verified; not for publication.",
    }
    result.update(overrides)
    return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.fixture = self.base / "fixture.json"
        self.write_fixture([])

    def write_fixture(self, items, **overrides):
        data = {"schema_version": "1.0", "mode": "mock", "items": items, "notes": ["SYNTHETIC TEST ONLY"]}
        data.update(overrides)
        write_json(self.fixture, data)

    def execute(self, **kwargs):
        opts = {"as_of": AS_OF.isoformat(), "allow_backfill": True}
        opts.update(kwargs)
        return run(self.root, self.fixture, **opts)

    def brief(self, result):
        return read_json(Path(result["run_dir"]) / "brief.json")

    def test_zero_items_is_valid_and_labeled(self):
        result = self.execute()
        brief = self.brief(result)
        self.assertEqual(brief["status"], "zero_items")
        self.assertEqual([a["hours"] for a in result["manifest"]["window_attempts"]], [24, 72, 168])
        report = (Path(result["run_dir"]) / "brief.md").read_text()
        self.assertIn("不能据此判断今天没有新闻", report)
        self.assertEqual(result["manifest"]["delivery"]["real_status"], "not_sent")

    def test_three_recent_items_stop_at_24_hours(self):
        self.write_fixture([item(str(i)) for i in range(3)])
        result = self.execute()
        self.assertEqual(self.brief(result)["window_hours"], 24)
        self.assertEqual(len(result["manifest"]["window_attempts"]), 1)

    def test_expands_to_72_hours(self):
        self.write_fixture([item("a"), item("b", age=30), item("c", age=70)])
        self.assertEqual(self.brief(self.execute())["window_hours"], 72)

    def test_expands_to_week_does_not_backfill(self):
        self.write_fixture([item("a"), item("b", age=100), item("c", age=168), item("old", age=169)])
        result = self.execute()
        self.assertEqual(self.brief(result)["window_hours"], 168)
        self.assertEqual(len(self.brief(result)["items"]), 3)
        self.assertEqual(result["manifest"]["exclusions"]["stale"], 1)
        self.assertEqual(len(list((self.root / "runs").glob("????-??-??"))), 1)

    def test_priority_and_cap(self):
        self.write_fixture([item("other", topic="other_ai_infra"), item("training", topic="manufacturing_embodied_training")] + [item(str(i), age=2) for i in range(5)])
        result = self.execute()
        self.assertEqual([x["topic"] for x in self.brief(result)["items"]], ["model_serving"] * 5 + ["manufacturing_embodied_training"])
        self.assertEqual(result["manifest"]["exclusions"]["trimmed"], 1)

    def test_future_items_excluded(self):
        future = AS_OF + timedelta(hours=2)
        self.write_fixture([item("future", published_at=future.isoformat(), retrieved_at=future.isoformat())])
        result = self.execute()
        self.assertEqual(self.brief(result)["items"], [])
        self.assertEqual(result["manifest"]["exclusions"]["future"], 1)

    def test_same_event_and_tracking_urls_dedup(self):
        self.write_fixture([item("one"), item("TWO", source_url="http://EXAMPLE.INVALID/one/?utm_source=test#ref"), item(" ONE ", source_url="https://example.invalid/another")])
        result = self.execute()
        self.assertEqual(len(self.brief(result)["items"]), 1)
        self.assertEqual(result["manifest"]["exclusions"]["duplicate_current"], 2)

    def test_prior_generated_brief_is_read(self):
        self.write_fixture([item("a", age=30, retrieved_at=(AS_OF - timedelta(days=1)).isoformat())])
        self.execute(as_of=(AS_OF - timedelta(days=1)).isoformat())
        result = self.execute()
        self.assertEqual(self.brief(result)["items"], [])
        self.assertEqual(len(result["manifest"]["history_files"]), 1)
        self.assertEqual(result["manifest"]["exclusions"]["duplicate_history"], 1)
        self.assertIn("SYNTHETIC TEST ONLY", (Path(result["run_dir"]) / "inputs.json").read_text())

    def test_legacy_markdown_history_urls(self):
        history = self.base / "history"
        history.mkdir()
        (history / "2026-10-29.md").write_text("旧报：[来源](https://example.invalid/one?utm_medium=x)\n")
        self.write_fixture([item("one")])
        result = self.execute(history_dir=history)
        self.assertEqual(self.brief(result)["items"], [])
        self.assertEqual(result["manifest"]["exclusions"]["duplicate_history"], 1)

    def test_legacy_markdown_balanced_parentheses(self):
        history = self.base / "history"
        history.mkdir()
        (history / "2026-10-29.md").write_text("旧报：[来源](https://example.invalid/one_(API))\n")
        self.write_fixture([item("one", source_url="https://example.invalid/one_(API)")])
        self.assertEqual(self.brief(self.execute(history_dir=history))["items"], [])

    def test_undated_legacy_history_fails_closed(self):
        history = self.base / "history"
        history.mkdir()
        (history / "brief.md").write_text("Unclear date")
        with self.assertRaisesRegex(BriefError, "must have YYYY"):
            self.execute(history_dir=history)

    def test_malformed_history_not_silently_ignored(self):
        history = self.base / "history"
        history.mkdir()
        (history / "2026-10-29.json").write_text("{}")
        with self.assertRaisesRegex(BriefError, "schema"):
            self.execute(history_dir=history)
        self.assertFalse((self.root / "runs" / "2026-10-30").exists())

    def test_expired_generated_history_does_not_block_new_day(self):
        path = self.root / "runs/2000-01-01/brief.json"
        write_json(path, {})
        self.assertEqual(self.brief(self.execute())["status"], "zero_items")

    def test_recent_malformed_generated_history_fails_closed(self):
        write_json(self.root / "runs/2026-10-29/brief.json", {})
        with self.assertRaisesRegex(BriefError, "schema"):
            self.execute()

    def test_dependency_lock_affects_implementation_fingerprint(self):
        project = self.base / "project"
        shutil.copytree(RESOURCE_ROOT, project)
        before = implementation_fingerprint(project)
        with (project / "requirements.lock").open("a") as stream:
            stream.write("\n# changed lock\n")
        self.assertNotEqual(before, implementation_fingerprint(project))

    def test_retry_reuses_immutable_day_snapshot(self):
        first = self.execute()
        second = self.execute(as_of=(AS_OF + timedelta(hours=1)).isoformat())
        self.assertTrue(second["reused"])
        self.assertEqual(first["manifest"], second["manifest"])

    def test_changed_fixture_cannot_replace_daily_snapshot(self):
        self.execute()
        self.write_fixture([item("new")])
        with self.assertRaisesRegex(BriefError, "different input snapshot"):
            self.execute()

    def test_artifact_tampering_blocks_reuse_and_simulation(self):
        result = self.execute()
        (Path(result["run_dir"]) / "brief.md").write_text("tampered")
        with self.assertRaisesRegex(BriefError, "Artifact missing or changed"):
            self.execute()
        with self.assertRaisesRegex(BriefError, "Artifact missing or changed"):
            simulate_delivery(self.root, "2026-10-30")

    def test_schema_rejects_missing_fields(self):
        candidate = item("a")
        del candidate["evidence"]
        self.write_fixture([candidate])
        with self.assertRaisesRegex(BriefError, "schema"):
            self.execute()

    def test_schema_rejects_unknown_fields(self):
        self.write_fixture([item("a", unsupported="not accepted")])
        with self.assertRaisesRegex(BriefError, "schema"):
            self.execute()

    def test_mock_cannot_claim_verified(self):
        self.write_fixture([item("a", verification="verified")])
        with self.assertRaisesRegex(BriefError, "mock_unverified"):
            self.execute()

    def test_schema_rejects_naive_time(self):
        self.write_fixture([item("a", published_at="2026-10-30T00:00:00")])
        with self.assertRaisesRegex(BriefError, "schema|timezone-aware"):
            self.execute()

    def test_schema_rejects_unretrieved_source(self):
        self.write_fixture([item("a", retrieved_at=(AS_OF - timedelta(hours=5)).isoformat())])
        with self.assertRaisesRegex(BriefError, "retrieved_at"):
            self.execute()

    def test_schema_rejects_credential_url(self):
        self.write_fixture([item("a", source_url="https://user:password@example.invalid/a")])
        with self.assertRaisesRegex(BriefError, "credentials"):
            self.execute()

    def test_schema_rejects_url_control_characters(self):
        self.write_fixture([item("a", source_url="https://example.invalid/a\nb")])
        with self.assertRaises(BriefError):
            self.execute()

    def test_pipeline_stays_offline(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), patch("os.getenv", side_effect=AssertionError("environment secret access forbidden")):
            result = self.execute()
            simulate_delivery(self.root, result["manifest"]["date"])

    def test_schema_rejects_status_item_inconsistency(self):
        brief = self.brief(self.execute())
        brief["status"] = "ready"
        with self.assertRaisesRegex(BriefError, "schema"):
            validate("brief", brief)

    def test_live_adapter_fails_before_creating_state(self):
        with self.assertRaisesRegex(BriefError, "NOT CONNECTED"):
            self.execute(adapter="live")
        self.assertFalse(self.root.exists())

    def test_live_delivery_cannot_send(self):
        with self.assertRaisesRegex(BriefError, "NOT CONNECTED"):
            simulate_delivery(self.root, "2026-10-30", adapter="slack")
        self.assertFalse(self.root.exists())

    def test_no_implicit_historical_backfill(self):
        with self.assertRaisesRegex(BriefError, "explicit --allow-backfill"):
            self.execute(as_of="2020-01-01T08:00:00+08:00", allow_backfill=False)

    def test_date_is_utc_plus_8(self):
        result = self.execute(as_of="2026-10-29T17:00:00+00:00", requested_date="2026-10-30")
        self.assertEqual(result["manifest"]["date"], "2026-10-30")

    def test_requested_date_must_match_and_cannot_traverse(self):
        with self.assertRaisesRegex(BriefError, "must equal"):
            self.execute(requested_date="../../escape")
        with self.assertRaisesRegex(BriefError, "YYYY-MM-DD"):
            simulate_delivery(self.root, "../../escape")

    def test_atomic_publish_failure_never_leaves_complete_run(self):
        with patch("ai_brief.pipeline.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.execute()
        self.assertFalse((self.root / "runs" / "2026-10-30").exists())
        self.assertFalse(list((self.root / "runs").glob("*.staging-*")))
        self.assertFalse(self.execute()["reused"])

    def test_root_lock_rejects_concurrent_run(self):
        with lock(self.root):
            with self.assertRaisesRegex(BriefError, "lock"):
                self.execute()

    def test_delivery_is_idempotent_and_never_claims_real_send(self):
        self.execute()
        first = simulate_delivery(self.root, "2026-10-30")
        second = simulate_delivery(self.root, "2026-10-30")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["ledger"]["attempts"], 1)
        self.assertEqual(second["ledger"]["status"], "simulated")
        self.assertEqual(len(list((self.root / "mock-outbox").glob("*.json"))), 1)
        manifest = read_json(self.root / "runs/2026-10-30/manifest.json")
        self.assertEqual(manifest["delivery"]["real_status"], "not_sent")

    def test_timeout_is_uncertain_and_not_automatically_retried(self):
        class TimeoutSender:
            def __init__(self): self.calls = 0
            def send(self, key, payload):
                self.calls += 1
                raise TimeoutError("may have accepted")
        self.execute()
        sender = TimeoutSender()
        with self.assertRaisesRegex(BriefError, "uncertain"):
            simulate_delivery(self.root, "2026-10-30", sender=sender)
        with self.assertRaisesRegex(BriefError, "refusing automatic resend"):
            simulate_delivery(self.root, "2026-10-30", sender=sender)
        self.assertEqual(sender.calls, 1)
        self.assertEqual(read_json(self.root / "runs/2026-10-30/delivery.simulation.json")["status"], "uncertain")

    def test_process_crash_in_flight_blocks_automatic_resend(self):
        class CrashingSender:
            def send(self, key, payload): raise KeyboardInterrupt()
        self.execute()
        with self.assertRaises(KeyboardInterrupt):
            simulate_delivery(self.root, "2026-10-30", sender=CrashingSender())
        with self.assertRaisesRegex(BriefError, "uncertain"):
            simulate_delivery(self.root, "2026-10-30")
        self.assertEqual(read_json(self.root / "runs/2026-10-30/delivery.simulation.json")["status"], "in_flight")

    def test_delivery_ledger_payload_mismatch_blocks(self):
        self.execute()
        path = self.root / "runs/2026-10-30/delivery.simulation.json"
        data = read_json(path)
        data["brief_sha256"] = "0" * 64
        write_json(path, data)
        with self.assertRaisesRegex(BriefError, "does not match"):
            simulate_delivery(self.root, "2026-10-30")

    def test_all_output_schemas_and_svg(self):
        result = self.execute()
        directory = Path(result["run_dir"])
        for schema, file in [("brief", "brief.json"), ("manifest", "manifest.json"), ("delivery", "delivery.simulation.json")]:
            validate(schema, read_json(directory / file))
        svg = ET.parse(directory / "overview.svg")
        self.assertEqual(svg.getroot().tag, "{http://www.w3.org/2000/svg}svg")
        self.assertFalse(svg.findall(".//{http://www.w3.org/2000/svg}script"))

    def test_markdown_escapes_untrusted_fixture_html(self):
        self.write_fixture([item("a", title="<script>alert(1)</script>")])
        result = self.execute()
        self.assertNotIn("<script>", (Path(result["run_dir"]) / "brief.md").read_text())

    def test_versioned_prompts_require_useful_not_boilerplate_caveats(self):
        for filename in ("research.md", "editor.md"):
            text = (RESOURCE_ROOT / "prompts" / filename).read_text()
            for required in ("v1.1", "不堆", "未独立复现", "作者报告", "影响选型", "紧邻", "空字符串", "来源"):
                self.assertIn(required, text)

    def test_no_material_limitation_omits_line_without_boilerplate(self):
        self.write_fixture([item("no-specific-limit", limitations="")])
        result = self.execute()
        text = (Path(result["run_dir"]) / "brief.md").read_text()
        self.assertNotIn("**选型限制：**", text)
        self.assertNotIn("未独立复现", text)
        self.assertNotIn("作者报告", text)
        self.assertIn("https://example.invalid/no-specific-limit", text)
        self.assertIn("模拟干跑", text)

    def test_material_limitation_is_adjacent_to_relevant_conclusion(self):
        limitation = "仅覆盖 H100 单模型离线吞吐，不能外推 T4 多模型在线延迟。"
        self.write_fixture([item("specific-limit", relevance="适合进一步比较服务调度方案。", limitations=limitation)])
        result = self.execute()
        lines = (Path(result["run_dir"]) / "brief.md").read_text().splitlines()
        index = next(i for i, line in enumerate(lines) if "**与当前工作的关系：**" in line)
        self.assertEqual(lines[index + 1], "- **选型限制：** " + limitation)

    def test_specific_attribution_and_uncertainty_are_not_removed(self):
        limitation = "作者报告的收益来自实验室单任务数据，未提供工厂多任务数据，不能推断产线成功率。"
        self.write_fixture([item("attributed-limit", limitations=limitation)])
        result = self.execute()
        self.assertIn(limitation, (Path(result["run_dir"]) / "brief.md").read_text())

    def test_cli_exit_codes_and_mock_only(self):
        with redirect_stdout(StringIO()) as output:
            code = main(["--state-dir", str(self.root), "run", "--fixture", str(self.fixture), "--as-of", AS_OF.isoformat(), "--allow-backfill"])
        self.assertEqual(code, 0)
        self.assertIn("not_sent", output.getvalue())
        with redirect_stderr(StringIO()) as error:
            code = main(["run", "--adapter", "live"])
        self.assertEqual(code, 2)
        self.assertIn("NOT CONNECTED", error.getvalue())

    def test_canonical_url_preserves_meaningful_query(self):
        self.assertEqual(canonical_url("http://EXAMPLE.invalid/a/?b=2&a=1&utm_term=x#s"), "https://example.invalid/a?a=1&b=2")
        self.assertNotEqual(canonical_url("https://example.invalid/a?id=1"), canonical_url("https://example.invalid/a?id=2"))


if __name__ == "__main__":
    unittest.main()
