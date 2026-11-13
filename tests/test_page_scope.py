"""合成页范围契约测试，不是真实发布证据。"""
from copy import deepcopy
import unittest
from ai_brief.coverage import notice_prefix
from ai_brief.research_audit import PAGE_CHECKS
from tests import test_layered_budget as layered


class PageScopeTests(unittest.TestCase):
    save = layered.LayeredBudgetTests.save
    check = layered.LayeredBudgetTests.check
    reject = layered.LayeredBudgetTests.reject
    artifact = layered.LayeredBudgetTests.artifact

    def setUp(self):
        layered.LayeredBudgetTests.setUp(self)
        ready = ["model-serving-gpu"]
        delivery = {"previous": None, "added": ready,
                    "pending": [{"id": page, "reason": "尚未完成该页研究，不代表无更新"}
                                for page in PAGE_CHECKS if page not in ready]}
        self.audit["page_scope"] = {"contract": "verified_pages_v1", "page_delivery": delivery,
                                    "checked_pages": ready, "previous_evidence": None}
        self.draft.update(schema_version="1.2", articles=[{"id": ready[0], "title": "合成测试"}],
                          page_delivery=deepcopy(delivery))
        self.draft["entries"][0]["article_id"] = ready[0]
        for page in delivery["pending"]:
            for name in PAGE_CHECKS[page["id"]]:
                self.audit["checks"][name] = {"status": "not_checked", "source_ids": [], "reason": "尚未检查"}
        self.notice()

    def notice(self):
        gaps = [name for name, check in self.audit["checks"].items()
                if check["status"] in {"not_checked", "failed"}]
        text = notice_prefix(gaps) + "缺页原因详见页面，未检查不代表无更新。"
        self.audit["coverage_notice"] = {"checks": gaps, "text": text}
        self.draft["subtitle"] = text

    def test_only_verified_page_with_honest_unchecked_pending_is_partial(self):
        result = self.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["coverage_incomplete"], result["coverage_unchecked"])
        self.assertEqual(result["page_scope"], self.audit["page_scope"])
        self.assertEqual(result["coverage_checked"], ["model_serving"])

    def test_legacy_without_scope_keeps_not_checked_blocker(self):
        del self.audit["page_scope"]
        self.audit["coverage_notice"] = None
        self.assertIn("required_coverage_not_checked", self.check()["blockers"])

    def test_ready_page_cannot_skip_checks_or_claim_failure(self):
        self.audit["checks"]["model_serving"]["status"] = "not_checked"
        self.reject("Ready pages")
        self.audit["checks"]["model_serving"]["status"] = "failed"
        self.reject("Ready pages")

    def test_page_partition_and_order_and_draft_are_strict(self):
        scope = deepcopy(self.audit["page_scope"])
        self.audit["page_scope"]["page_delivery"]["pending"].reverse()
        self.reject("canonical")
        self.audit["page_scope"] = deepcopy(scope)
        self.draft["page_delivery"]["pending"][0]["reason"] = "不同原因"
        self.reject("Draft page delivery")
        self.draft["page_delivery"] = deepcopy(scope["page_delivery"])
        self.draft["entries"][0]["article_id"] = "hardware-status"
        self.reject("entries must belong")
        self.draft["entries"][0]["article_id"] = "model-serving-gpu"
        self.draft["articles"].append({"id": "hardware-status"})
        self.reject("articles must be exactly")

    def test_successor_without_evidence_remains_rejected(self):
        self.audit["page_scope"]["page_delivery"]["previous"] = {
            "request_id": "morning-20261106-r1", "bundle_sha256": "a" * 64}
        self.reject("Previous page evidence")
        self.audit["page_scope"]["page_delivery"]["previous"] = None
        self.draft["revision"] = 2
        self.reject("revision 1")

    def test_missing_model_or_budget_failure_still_blocks(self):
        self.audit["model_calls"] = []
        self.assertIn("model_stage_missing", self.check()["blockers"])

    def test_successful_work_budget_still_blocks(self):
        self.audit["limits"]["run_seconds"] = 1
        self.assertIn("run_budget_exceeded", self.check()["blockers"])

    def test_no_qualifying_ready_check_still_requires_real_success_source(self):
        self.audit["checks"]["model_serving"].update(status="no_qualifying_items", source_ids=[])
        self.reject("successful source read")

    def test_pending_failed_and_unchecked_remain_distinct(self):
        name = "hardware"
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-" + name)
        source.update(status="failed", content_sha256=None, error="真实测试故障", elapsed_seconds=1)
        source["attempt_records"] = [dict(status="failed", elapsed_seconds=1, error=source["error"], reserved_seconds=180)]
        self.audit["checks"][name].update(status="failed", failure_kind="transport", source_ids=[source["id"]])
        self.notice()
        result = self.check()
        self.assertIn(name, result["coverage_incomplete"])
        self.assertNotIn(name, result["coverage_unchecked"])
        self.assertEqual(result["readiness"], "partial")

    def test_notice_cannot_hide_unchecked_pending(self):
        self.audit["coverage_notice"]["checks"].pop()
        self.reject("every incomplete check")

    def test_unattempted_pending_cannot_be_relabelled_failed(self):
        self.audit["checks"]["hardware"].update(status="failed", failure_kind="transport")
        self.notice()
        self.reject("attempted matching source")

    def test_scope_rejected_for_pre_layered_schema(self):
        self.audit["schema_version"] = "1.3"
        self.reject("metadata schema")

    def test_architecture_and_engine_require_all_official_groups(self):
        for page in ("model-architecture-status", "inference-engines"):
            self.audit["page_scope"]["checked_pages"] = [page]
            self.audit["page_scope"]["page_delivery"]["added"] = [page]
            self.audit["page_scope"]["page_delivery"]["pending"] = [
                {"id": p, "reason": "尚未完成"} for p in PAGE_CHECKS if p != page]
            self.audit["checks"][PAGE_CHECKS[page][0]]["status"] = "checked"
            self.reject("Ready pages")

    def test_pending_reasons_obey_public_text_constraints(self):
        for reason in (" padded", "padded ", "x\n", "<hidden>", "\u2028", "\u2029", "\ufeff", "\ud800", "中" * 101):
            with self.subTest(reason=repr(reason)):
                self.audit["page_scope"]["page_delivery"]["pending"][0]["reason"] = reason
                # JSON 转义代理字符必须安全进入验证器。
                if reason == "\ud800":
                    self.audit["page_scope"]["page_delivery"]["pending"][0]["reason"] = "安全占位"
                    self.save()
                    path = self.root / "audit.json"
                    value = deepcopy(self.audit)
                    value["page_scope"]["page_delivery"]["pending"][0]["reason"] = reason
                    import json
                    from ai_brief.research_audit import validate_audit
                    from ai_brief.storage import BriefError
                    path.write_text(json.dumps(value, ensure_ascii=True))
                    with self.assertRaisesRegex(BriefError, "forbidden"):
                        validate_audit(self.root)
                else:
                    self.reject("Pending page reason")

    def test_ready_page_requires_at_least_one_bound_entry(self):
        self.draft["entries"] = []
        self.reject("entries must belong")

    def test_page_scope_requires_explicit_partial_policy(self):
        self.audit.pop("coverage_policy")
        self.reject("partial coverage policy|metadata schema")
