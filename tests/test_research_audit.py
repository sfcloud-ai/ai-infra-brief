"""私有审计的离线合同回归；合成样本不是生产研究或送达证据。"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_brief.cli import main
from ai_brief.contracts import RESOURCE_ROOT
from ai_brief.research_audit import validate_audit
from ai_brief.storage import BriefError


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class ResearchAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.joinpath("evidence").mkdir()
        self.root.joinpath("prompts").mkdir()
        self.content = "合成测试来源：修复缓存生命周期错误。不是生产证据。"
        self.root.joinpath("evidence/source.txt").write_bytes(self.content.encode())
        self.candidate = {
            "event_key": "cache-fix", "title": "合成测试", "topic": "model_serving",
            "published_at": "2026-11-06T06:00:00+08:00", "retrieved_at": "2026-11-06T07:22:00+08:00",
            "source_url": "https://example.org/change", "source_name": "测试来源", "source_type": "primary",
            "verification": "verified", "evidence": "修复缓存生命周期错误", "summary": "合成测试事实",
            "relevance": "合成测试相关性", "limitations": "",
        }
        self.candidates = {"schema_version": "1.0", "mode": "live", "items": [self.candidate], "notes": []}
        self.draft = {"date": "2026-11-06", "revision": 1, "entries": [{"event_key": "cache-fix", "source_urls": [self.candidate["source_url"]], "update_of": None}]}
        self.history = {"schema_version": "1.0", "reports": [], "entries": [{"id": "old-cache", "event_key": "previous-event"}]}
        self.audit = {
            "schema_version": "1.0", "scope": "new_daily", "date": "2026-11-06", "as_of": "2026-11-06T07:20:00+08:00",
            "started_at": "2026-11-06T07:20:00+08:00", "finished_at": "2026-11-06T07:30:00+08:00", "window_hours": 24,
            "inputs": {"history_commit": "a" * 40, "prompt_commit": "b" * 40, "prompt_hashes": {},
                       "history_sha256": "", "candidates_sha256": "", "draft_sha256": ""},
            "limits": {"source_attempts": 3, "model_attempts": 2, "operation_seconds": 120, "run_seconds": 1200},
            "sources": [{"id": "source", "url": self.candidate["source_url"], "status": "ok", "attempts": 1,
                         "elapsed_seconds": 10, "retrieved_at": self.candidate["retrieved_at"],
                         "published_at": self.candidate["published_at"], "content_sha256": hashlib.sha256(self.content.encode()).hexdigest(), "error": ""}],
            "checks": {name: {"status": "checked", "source_ids": ["source"], "reason": "合成覆盖记录"}
                       for name in ("vllm", "sglang", "deepinfer", "vllm_blog", "sglang_blog",
                                    "deepseek_articles", "deepinfer_articles")},
            "status_entries": [],
            "decisions": [{"event_key": "cache-fix", "disposition": "selected", "source_ids": ["source"],
                           "reason": "合成入选理由", "history_entry_ids": []}],
            "model_calls": [{"stage": stage, "status": "ok", "attempts": 1, "elapsed_seconds": 20,
                             "model": {"name": None, "version": None, "unavailable_reason": "测试环境未暴露模型信息"}, "error": ""}
                            for stage in ("research", "editor")],
        }
        for name in ("research.md", "editor.md", "sources.md", "delivery.md", "audit.md"):
            data = (RESOURCE_ROOT / "prompts" / name).read_bytes()
            self.root.joinpath("prompts", name).write_bytes(data)
            self.audit["inputs"]["prompt_hashes"][name] = hashlib.sha256(data).hexdigest()
        self.save()

    def save(self):
        for name, value, field in (("candidates.json", self.candidates, "candidates_sha256"),
                                   ("history.json", self.history, "history_sha256")):
            data = encoded(value)
            self.root.joinpath(name).write_bytes(data)
            self.audit["inputs"][field] = hashlib.sha256(data).hexdigest()
        if self.draft is not None:
            data = encoded(self.draft)
            self.root.joinpath("draft.json").write_bytes(data)
            self.audit["inputs"]["draft_sha256"] = hashlib.sha256(data).hexdigest()
        else:
            self.audit["inputs"]["draft_sha256"] = None
        self.root.joinpath("audit.json").write_bytes(encoded(self.audit))

    def check(self):
        self.save()
        return validate_audit(self.root)

    def reject(self, pattern):
        with self.assertRaisesRegex(BriefError, pattern):
            self.check()

    def test_secondary_only_selected_or_updated_candidates_are_rejected(self):
        self.candidate["source_type"] = "secondary"
        self.reject("require primary source evidence")
        self.audit["decisions"][0].update(disposition="update", history_entry_ids=["old-cache"])
        self.history["entries"][0]["event_key"] = "cache-fix"
        self.draft["entries"][0]["update_of"] = "old-cache"
        self.reject("require primary source evidence")

    def test_correction_scope_is_explicitly_unsupported_not_reclassified(self):
        self.history["reports"] = [{"date": "2026-11-06", "revision": 1}]
        self.reject("Same-day corrections are outside")
        self.history["reports"] = []
        self.draft["revision"] = 2
        self.reject("new daily revision 1 only")
        self.draft["revision"] = True
        self.reject("new daily revision 1 only")
        self.draft["revision"] = 1
        self.audit["scope"] = "correction"
        self.reject("Invalid research-audit metadata schema")

    def add_status(self):
        self.audit["status_entries"] = [{"event_key": "hardware-status-20261106", "source_ids": ["source"],
                                         "reason": "静态规格复核，不是新进展"}]
        self.draft["entries"].append({"event_key": "hardware-status-20261106",
                                      "source_urls": [self.candidate["source_url"]], "update_of": None})

    def test_status_entries_have_independent_evidence_without_invented_publication(self):
        self.add_status()
        self.candidates["items"] = []
        self.audit["decisions"] = []
        self.draft["entries"].pop(0)
        self.audit["sources"][0]["published_at"] = None
        result = self.check()
        self.assertEqual(result["readiness"], "zero_items")
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["status_entry_count"], 1)
        self.assertFalse(result["delivery_allowed"])

    def test_status_cannot_reclassify_a_news_candidate_or_use_unread_evidence(self):
        self.add_status()
        self.audit["status_entries"][0]["event_key"] = "cache-fix"
        self.reject("distinct from candidates")
        self.audit["status_entries"][0]["event_key"] = "hardware-status-20261106"
        self.audit["status_entries"][0]["source_ids"] = ["unread"]
        self.reject("unique and known")
        self.audit["status_entries"][0]["source_ids"] = []
        self.reject("Invalid research-audit metadata schema")

    def test_status_draft_preserves_coverage_binding_and_cannot_create_update(self):
        self.add_status()
        self.assertEqual(self.check()["readiness"], "ready")
        self.draft["entries"][-1]["update_of"] = "old-cache"
        self.reject("unaudited update relationship")
        self.draft["entries"][-1]["update_of"] = None
        self.draft["entries"][-1]["source_urls"] = ["https://example.org/unread"]
        self.reject("sources differ")
        self.draft["entries"].pop()
        self.reject("every selected event and coverage status")

    def test_update_cannot_point_to_an_unrelated_historical_event(self):
        self.audit["decisions"][0].update(disposition="update", history_entry_ids=["old-cache"])
        self.draft["entries"][0]["update_of"] = "old-cache"
        self.reject("same historical event")
        self.history["entries"][0]["event_key"] = "cache-fix"
        self.assertEqual(self.check()["readiness"], "ready")

    def test_repeated_event_needs_audited_update_and_unambiguous_history(self):
        self.history["entries"][0]["event_key"] = "cache-fix"
        self.reject("requires an audited update")
        self.history["entries"].append(dict(self.history["entries"][0]))
        self.reject("Duplicate history entry ID")

    def test_article_coverage_is_required_separately_from_repository_checks(self):
        for name in ("vllm_blog", "sglang_blog", "deepseek_articles", "deepinfer_articles"):
            with self.subTest(check=name):
                original = self.audit["checks"].pop(name)
                self.reject("Invalid research-audit metadata schema")
                self.audit["checks"][name] = {"status": "not_checked", "source_ids": [], "reason": "尚未读取技术文章"}
                result = self.check()
                self.assertEqual(result["readiness"], "blocked")
                self.assertIn("required_coverage_incomplete", result["blockers"])
                self.audit["checks"][name] = original

    def test_read_only_success_never_grants_delivery_or_changes_files(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = validate_audit(self.root)
        self.assertEqual(result["readiness"], "ready")
        self.assertFalse(result["delivery_allowed"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertNotIn("测试环境", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("example.org", json.dumps(result))

    def test_zero_items_requires_completed_checks_and_model_stages(self):
        self.candidates["items"] = []
        self.audit["decisions"] = []
        self.draft["entries"] = []
        for check in self.audit["checks"].values():
            check["status"] = "no_qualifying_items"
        self.assertEqual(self.check()["readiness"], "zero_items")
        self.audit["checks"]["deepinfer"]["status"] = "failed"
        self.assertEqual(self.check()["readiness"], "blocked")

    def test_failed_model_with_a_draft_is_still_blocked(self):
        self.audit["model_calls"][1].update(status="failed", error="合成失败")
        self.assertIn("model_stage_failed", self.check()["blockers"])

    def test_missing_editor_and_draft_are_recordable_blockers(self):
        self.audit["model_calls"].pop()
        self.draft = None
        self.assertEqual(self.check()["blockers"], ["draft_missing", "model_stage_missing"])

    def test_each_budget_is_enforced(self):
        changes = (("source_attempts", 0, "source_budget_exceeded"), ("model_attempts", 0, "model_budget_exceeded"))
        for name, _, blocker in changes:
            with self.subTest(name=name):
                original = self.audit["limits"][name]
                self.audit["limits"][name] = 1
                record = self.audit["sources"][0] if name == "source_attempts" else self.audit["model_calls"][0]
                record["attempts"] = 2
                self.assertIn(blocker, self.check()["blockers"])
                record["attempts"] = 1
                self.audit["limits"][name] = original
        self.audit["limits"]["operation_seconds"] = 5
        self.audit["limits"]["run_seconds"] = 60
        self.assertEqual(self.check()["blockers"], ["model_budget_exceeded", "run_budget_exceeded", "source_budget_exceeded"])

    def test_hashes_bind_actual_bytes(self):
        for name in ("candidates.json", "draft.json", "history.json", "evidence/source.txt", "prompts/editor.md"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(BriefError, "fingerprint mismatch"):
                    validate_audit(self.root)
                path.write_bytes(original)

    def test_missing_source_text_and_fabricated_excerpt_fail(self):
        self.candidate["evidence"] = "不在来源中的内容"
        self.reject("must occur")
        self.candidate["evidence"] = "修复缓存生命周期错误"
        self.root.joinpath("evidence/source.txt").unlink()
        self.reject("Missing audit file")

    def test_candidate_dates_must_match_source_dates(self):
        self.candidate["retrieved_at"] = "2026-11-06T07:23:00+08:00"
        self.reject("matching publication")

    def test_retrieval_cannot_replace_missing_publication(self):
        self.audit["sources"][0]["published_at"] = None
        self.reject("matching publication")

    def test_unread_failed_sources_cannot_support_coverage(self):
        self.audit["sources"][0].update(status="timeout", content_sha256=None, error="合成超时")
        self.reject("Completed coverage")

    def test_explicit_alternative_can_resolve_a_failed_discovery_source(self):
        source = dict(self.audit["sources"][0], id="failed", status="failed", content_sha256=None, error="合成入口失败")
        self.audit["sources"].append(source)
        self.audit["checks"]["vllm"]["source_ids"].append("failed")
        self.assertEqual(self.check()["readiness"], "ready")

    def test_selected_evidence_cannot_reference_a_failed_source(self):
        source = dict(self.audit["sources"][0], id="failed", status="failed", content_sha256=None, error="合成失败")
        self.audit["sources"].append(source)
        self.audit["decisions"][0]["source_ids"].append("failed")
        self.reject("failed source")

    def test_all_candidates_require_a_decision(self):
        self.audit["decisions"] = []
        self.reject("Every candidate")

    def test_duplicate_ids_and_unknown_references_fail(self):
        self.audit["sources"].append(dict(self.audit["sources"][0]))
        self.reject("Duplicate source")
        self.audit["sources"].pop()
        self.audit["checks"]["vllm"]["source_ids"] = ["missing"]
        self.reject("unique and known")

    def test_draft_cannot_omit_selected_or_add_unknown_events(self):
        self.draft["entries"] = []
        self.reject("every selected")
        self.draft["entries"] = [{"event_key": "unknown", "source_urls": []}]
        self.reject("excluded, unknown")

    def test_draft_cannot_add_unread_source_urls(self):
        self.draft["entries"][0]["source_urls"].append("https://example.org/unread")
        self.reject("sources differ")

    def test_selected_entry_cannot_introduce_an_unaudited_update(self):
        self.draft["entries"][0]["update_of"] = "old-cache"
        self.reject("unaudited update relationship")

    def test_duplicate_and_update_require_known_history(self):
        self.history["entries"][0]["event_key"] = "cache-fix"
        self.audit["decisions"][0]["disposition"] = "update"
        self.reject("history entry references")
        self.audit["decisions"][0]["history_entry_ids"] = ["missing"]
        self.reject("retained index")
        self.audit["decisions"][0]["history_entry_ids"] = ["old-cache"]
        self.reject("history relationship")
        self.draft["entries"][0]["update_of"] = "old-cache"
        self.assertEqual(self.check()["readiness"], "ready")
        self.audit["decisions"][0]["disposition"] = "duplicate"
        self.draft["entries"] = []
        self.assertEqual(self.check()["readiness"], "zero_items")

    def test_old_and_future_selected_items_are_rejected(self):
        for value in ("2026-11-04T06:00:00+08:00", "2026-11-06T07:21:00+08:00"):
            self.candidate["published_at"] = value
            self.audit["sources"][0]["published_at"] = value
            self.reject("outside the research window")

    def test_run_dates_use_asia_shanghai_and_read_times_are_bounded(self):
        self.audit["date"] = "2026-11-05"
        self.reject("match as_of")
        self.audit["date"] = "2026-11-06"
        self.audit["sources"][0]["retrieved_at"] = "2026-11-06T08:00:00+08:00"
        self.reject("outside this run")

    def test_unknown_model_version_requires_honest_reason(self):
        self.audit["model_calls"][0]["model"]["unavailable_reason"] = ""
        self.reject("honest explanation")

    def test_mock_input_is_never_accepted_as_live_audit(self):
        self.candidates["mode"] = "mock"
        self.candidate["verification"] = "mock_unverified"
        self.reject("not mock fixtures")

    def test_duplicate_json_keys_and_nonfinite_numbers_fail(self):
        original = self.root.joinpath("audit.json").read_bytes()
        self.root.joinpath("audit.json").write_bytes(b'{"schema_version":"1.0","schema_version":"1.0"}')
        with self.assertRaisesRegex(BriefError, "Invalid audit JSON"):
            validate_audit(self.root)
        self.root.joinpath("audit.json").write_bytes(original.replace(b'"elapsed_seconds": 10', b'"elapsed_seconds": NaN'))
        with self.assertRaisesRegex(BriefError, "Invalid audit JSON"):
            validate_audit(self.root)

    def test_schema_rejects_unknown_fields_missing_stages_and_path_ids(self):
        self.audit["secret"] = "合成私有标记"
        self.reject("metadata schema")
        del self.audit["secret"]
        self.audit["sources"][0]["id"] = "../outside"
        self.reject("metadata schema")

    def test_symlinked_evidence_is_rejected(self):
        path = self.root / "evidence/source.txt"
        path.unlink()
        path.symlink_to(self.root / "candidates.json")
        self.reject("must not be symlinks")

    def test_source_urls_reject_credentials(self):
        self.audit["sources"][0]["url"] = "https://user:password@example.org/change"
        self.reject("no credentials")

    def test_cli_timestamp_errors_do_not_echo_private_metadata(self):
        fields = [(self.audit, key) for key in ("as_of", "started_at", "finished_at")]
        fields += [(self.audit["sources"][0], key) for key in ("published_at", "retrieved_at")]
        fields += [(self.candidate, key) for key in ("published_at", "retrieved_at")]
        for record, key in fields:
            with self.subTest(key=key):
                original = record[key]
                record[key] = "/private/task-customer-identity"
                self.save()
                output = StringIO()
                with redirect_stderr(output):
                    self.assertEqual(main(["audit-research", str(self.root)]), 2)
                self.assertNotIn("task-customer-identity", output.getvalue())
                record[key] = original

    def test_cli_filesystem_errors_do_not_leak_private_paths(self):
        private = self.root / "private-task-identity"
        output = StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["audit-research", str(private)]), 2)
        self.assertIn("Cannot read private audit package", output.getvalue())
        self.assertNotIn(str(private), output.getvalue())
        self.assertNotIn("private-task-identity", output.getvalue())
        private.symlink_to(private)
        output = StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["audit-research", str(private)]), 2)
        self.assertIn("Cannot read private audit package", output.getvalue())
        self.assertNotIn("private-task-identity", output.getvalue())
        output = StringIO()
        with patch.object(Path, "open", side_effect=PermissionError(f"unreadable: {private}")):
            with redirect_stderr(output):
                self.assertEqual(main(["audit-research", str(self.root)]), 2)
        self.assertIn("Cannot read private audit package", output.getvalue())
        self.assertNotIn(str(private), output.getvalue())

    def test_cli_exit_codes_do_not_write_state_or_leak_data(self):
        for status, expected in (("ok", 0), ("failed", 3)):
            self.audit["model_calls"][1].update(status=status, error="合成私有标记" if status == "failed" else "")
            self.save()
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--state-dir", str(self.root / "unused"), "audit-research", str(self.root)])
            self.assertEqual(code, expected)
            self.assertFalse(self.root.joinpath("unused").exists())
            self.assertNotIn("合成私有标记", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["delivery_allowed"])
        self.root.joinpath("audit.json").write_text("{}")
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["audit-research", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
