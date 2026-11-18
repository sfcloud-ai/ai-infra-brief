"""冻结提示词决定整期六专题交付门槛；样本不代表实际成稿。"""
import hashlib
import json
import unittest

from ai_brief.contracts import RESOURCE_ROOT
from ai_brief.research_audit import PAGE_CHECKS
from tests import test_community_discovery as community
from tests import test_layered_budget as layered
from tests import test_page_scope as scoped
from tests import test_research_audit as original_audit


class CompleteEditionTests(unittest.TestCase):
    save = layered.LayeredBudgetTests.save
    check = layered.LayeredBudgetTests.check
    artifact = layered.LayeredBudgetTests.artifact
    notice = scoped.PageScopeTests.notice

    def marker(self):
        data = b"Group-Delivery: complete_six_topics_v1"
        (self.root / "prompts" / "audit.md").write_bytes(data)
        self.audit["inputs"]["prompt_hashes"]["audit.md"] = hashlib.sha256(data).hexdigest()

    def discovery_edition(self):
        case = community.CommunityDiscoveryTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        prompt = (RESOURCE_ROOT / "prompts/audit.md").read_bytes()
        (case.root / "prompts/audit.md").write_bytes(prompt)
        case.audit["inputs"]["prompt_hashes"]["audit.md"] = hashlib.sha256(prompt).hexdigest()
        case.draft.update(schema_version="1.0", title="合成晨报", subtitle="合成副标题", images=[], poster="assets/overview.png")
        case.draft["articles"] = [dict(id=page, title=page) for page in PAGE_CHECKS]
        case.draft["entries"][0]["article_id"] = "model-serving-gpu"
        for page in list(PAGE_CHECKS)[1:]:
            ref = "coverage-" + PAGE_CHECKS[page][0]
            uri = next(src["url"] for src in case.audit["sources"] if src["id"] == ref)
            key = "status-" + page
            case.audit["status_entries"].append(dict(event_key=key, source_ids=[ref], reason="已核对合成来源"))
            case.draft["entries"].append(dict(event_key=key, article_id=page, source_urls=[uri]))
        return case

    def test_recovered_check_can_coexist_with_another_disclosed_gap(self):
        case = self.discovery_edition()
        case.audit["checks"]["model_serving"]["source_ids"].append("source")
        failed = layered.LayeredBudgetTests.failed(case, kind="transport")
        failed["attempt_records"][0].update(status="failed", error=failed["error"])
        output, original, call = case.retained_detail_resolution()
        case.bind_retained_output(output, original, call)
        result = case.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["coverage_incomplete"], ["qwen_blog"])
        self.assertIn("research_coverage_partially_resolved", {w["code"] for w in result["warnings"]})
        self.assertEqual(output["checks"]["qwen_blog"]["status"], "failed")

    def test_other_topics_evidence_cannot_supply_the_only_entry_for_a_page(self):
        case = self.discovery_edition()
        case.audit["checks"]["model_serving"]["source_ids"].append("source")
        failed = layered.LayeredBudgetTests.failed(case, name="embodied_training", kind="transport")
        failed["attempt_records"][0].update(status="failed", error=failed["error"])
        entry = next(e for e in case.draft["entries"] if e["article_id"] == "embodied-training")
        status = next(s for s in case.audit["status_entries"] if s["event_key"] == entry["event_key"])
        hardware = next(s for s in case.audit["sources"] if s["id"] == "coverage-hardware")
        status["source_ids"], entry["source_urls"] = [hardware["id"]], [hardware["url"]]
        self.assertIn("complete_edition_required", case.check()["blockers"])

    def test_full_edition_capacity_preserves_original_reads_and_individual_limits(self):
        case = self.discovery_edition()
        case.audit["checks"]["model_serving"]["source_ids"].append("source")
        ids = ("source", "coverage-model_serving")
        evidence = case.root / "evidence"
        original = {key: (evidence / (key + ".txt")).read_bytes() for key in ids}

        def pad(key, length):
            data = original[key].ljust(length, b" ")
            (evidence / (key + ".txt")).write_bytes(data)
            next(s for s in case.audit["sources"] if s["id"] == key)["content_sha256"] = hashlib.sha256(data).hexdigest()

        for key in ids:
            pad(key, 11_000_000)
        self.assertEqual(case.check()["readiness"], "ready")
        prompt = case.root / "prompts" / "audit.md"
        current = prompt.read_bytes()
        marker = b"Evidence-Package: whole_edition_32mb_v1"
        historical = current.replace(marker, b"")
        self.assertNotEqual(current, historical)
        prompt.write_bytes(historical)
        case.audit["inputs"]["prompt_hashes"]["audit.md"] = hashlib.sha256(historical).hexdigest()
        case.reject("Audit package exceeds size limit")
        prompt.write_bytes(current)
        case.audit["inputs"]["prompt_hashes"]["audit.md"] = hashlib.sha256(current).hexdigest()
        for key in ids:
            pad(key, 16_000_000)
        case.reject("Audit package exceeds size limit")
        pad(ids[1], len(original[ids[1]]))
        pad(ids[0], 20_000_001)
        case.reject("Audit file exceeds size limit")

    def test_former_single_page_is_blocked_under_current_contract(self):
        scoped.PageScopeTests.setUp(self)
        self.marker()
        self.assertIn("complete_edition_required", self.check()["blockers"])

    def test_old_prompt_cannot_publish_a_partial_first_edition_on_cutover_day(self):
        case = original_audit.ResearchAuditTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.assertEqual(case.check()["readiness"], "ready")
        for name in ("audit", "candidates", "draft"):
            data = json.dumps(getattr(case, name)).replace("2026-11-06", "2026-11-19")
            setattr(case, name, json.loads(data))
        self.assertIn("complete_edition_required", case.check()["blockers"])

    def test_current_full_prompt_blocks_incomplete_discovery_edition(self):
        original = community.CommunityDiscoveryTests("runTest")
        original.setUp()
        self.addCleanup(original.doCleanups)
        raw = (RESOURCE_ROOT / "prompts/audit.md").read_bytes()
        (original.root / "prompts/audit.md").write_bytes(raw)
        original.audit["inputs"]["prompt_hashes"]["audit.md"] = hashlib.sha256(raw).hexdigest()
        self.assertIn("complete_edition_required", original.check()["blockers"])

    def test_missing_draft_with_all_checks_complete_is_blocked(self):
        layered.LayeredBudgetTests.setUp(self)
        self.marker()
        self.draft = None
        self.assertIn("complete_edition_required", self.check()["blockers"])

    def test_six_pages_with_bound_content_remain_ready(self):
        layered.LayeredBudgetTests.setUp(self)
        self.marker()
        pages = list(PAGE_CHECKS)
        delivery = dict(previous=None, added=pages, pending=[])
        self.audit["page_scope"] = dict(contract="verified_pages_v1", page_delivery=delivery,
                                        checked_pages=pages, previous_evidence=None)
        self.draft.update(schema_version="1.2", articles=[dict(id=page, title=page) for page in pages],
                          page_delivery=delivery)
        self.draft["entries"][0]["article_id"] = pages[0]
        refs = self.audit["checks"]["model_serving"]["source_ids"]
        if "source" not in refs:
            refs.append("source")
        for page in pages[1:]:
            source_id = "coverage-" + PAGE_CHECKS[page][0]
            source = next(src for src in self.audit["sources"] if src["id"] == source_id)
            key = "event-" + page
            self.audit["status_entries"].append(dict(event_key=key, source_ids=[source_id],
                                                     reason="已核对该主题来源，合成状态记录"))
            self.draft["entries"].append(dict(event_key=key, article_id=page, source_urls=[source["url"]]))
        self.assertIn("complete_edition_required", self.check()["blockers"])
        self.audit.pop("page_scope")
        self.audit["coverage_policy"] = "strict"
        self.draft.pop("page_delivery")
        self.draft.update(title="合成晨报", subtitle="合成副标题", images=[], poster="assets/overview.png")
        self.assertIn("complete_edition_required", self.check()["blockers"])
        self.draft["schema_version"] = "1.0"
        self.assertEqual(self.check()["readiness"], "ready")
        self.draft["revision_notice"] = {}
        self.assertIn("complete_edition_required", self.check()["blockers"])
        self.draft.pop("revision_notice")
        small_entry = next(item for item in self.draft["entries"] if item["article_id"] == "small-model-status")
        check = self.audit["checks"]["small_models"]
        ref = "coverage-qwen_blog"
        official = next(src for src in self.audit["sources"] if src["id"] == ref)
        status = next(item for item in self.audit["status_entries"] if item["event_key"] == small_entry["event_key"])
        check["source_ids"].append(ref)
        status["source_ids"], small_entry["source_urls"] = [ref], [official["url"]]
        self.assertEqual(self.check()["readiness"], "ready")
        check["source_ids"].remove(ref)
        base_ref = "coverage-small_models"
        status["source_ids"] = [base_ref]
        small_entry["source_urls"] = [next(src["url"] for src in self.audit["sources"] if src["id"] == base_ref)]
        previous_items, previous_decisions = self.candidates["items"], self.audit["decisions"]
        self.candidates["items"], self.audit["decisions"] = [], []
        entry = self.draft["entries"][0]
        original_urls = entry["source_urls"]
        status_source = next(item for item in self.audit["sources"] if item["id"] == "coverage-model_serving")
        entry["source_urls"] = [status_source["url"]]
        self.audit["status_entries"].append(dict(event_key=entry["event_key"], source_ids=[status_source["id"]],
                                                 reason="已核对当期状态"))
        self.assertEqual(self.check()["readiness"], "zero_items")
        self.audit["status_entries"].pop()
        entry["source_urls"] = original_urls
        self.candidates["items"], self.audit["decisions"] = previous_items, previous_decisions
        self.audit["coverage_policy"] = "publish_verified_with_gaps"
        source = layered.LayeredBudgetTests.failed(self, kind="transport")
        source["attempt_records"][0].update(status="failed", error=source["error"])
        partial = self.check()
        self.assertEqual(partial["readiness"], "partial")
        self.assertEqual(partial["blockers"], [])
        self.assertFalse(partial["delivery_allowed"])
