"""离线测试：哈希不能替代宿主认证的真实发送回执。"""
from copy import deepcopy
import base64
import json
import hashlib
import unittest
from tests import test_page_scope as scoped
from tests.test_research_audit import encoded
from ai_brief.research_audit import validate_audit, PAGE_CHECKS


class PageSuccessorTests(unittest.TestCase):
    save = scoped.PageScopeTests.save
    check = scoped.PageScopeTests.check
    reject = scoped.PageScopeTests.reject
    artifact = scoped.PageScopeTests.artifact
    notice = scoped.PageScopeTests.notice

    def test_previous_metadata_schema_error_is_sanitized_in_cli(self):
        from ai_brief.cli import main
        from contextlib import redirect_stderr
        from io import StringIO
        self.save()
        metadata = deepcopy(self.previous_metadata)
        metadata["as_of"] = "PRIVATE_SENTINEL"
        raw = encoded(metadata)
        self.root.joinpath("previous/metadata.json").write_bytes(raw)
        summary = deepcopy(self.previous_summary)
        summary["evidence_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
        summary_raw = encoded(summary)
        self.root.joinpath("previous/audit.json").write_bytes(summary_raw)
        self.audit["page_scope"]["previous_evidence"]["audit_sha256"] = hashlib.sha256(summary_raw).hexdigest()
        self.save()
        output = StringIO()
        with redirect_stderr(output):
            self.assertEqual(main(["audit-research", str(self.root)]), 2)
        self.assertIn("Invalid previous research-audit metadata schema", output.getvalue())
        self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())

    def setUp(self):
        scoped.PageScopeTests.setUp(self)
        self.promote("hardware-status")

    def promote(self, page):
        self.save()
        summary = validate_audit(self.root)
        old_report, old_metadata = deepcopy(self.draft), deepcopy(self.audit)
        prior = self.root / "previous"
        prior.mkdir(exist_ok=True)
        summary_bytes = encoded(summary)
        prior.joinpath("audit.json").write_bytes(summary_bytes)
        prior.joinpath("metadata.json").write_bytes(self.root.joinpath("audit.json").read_bytes())
        prior.joinpath("report.json").write_bytes(self.root.joinpath("draft.json").read_bytes())
        revision = old_report["revision"] + 1
        sha = hashlib.sha256(summary_bytes).hexdigest()
        evidence = dict(request_id="morning-20261106-r" + str(revision-1),
                        bundle_sha256="a"*64, audit_sha256=sha, archive_sha256="b"*64, report_commit="c"*40)
        delivery = dict(previous={key: evidence[key] for key in ("request_id", "bundle_sha256")},
                        added=[page], pending=[item for item in old_report["page_delivery"]["pending"] if item["id"] != page])
        self.audit["page_scope"] = dict(contract="verified_pages_v1", page_delivery=deepcopy(delivery),
                                       checked_pages=[page], previous_evidence=evidence)
        self.draft.update(revision=revision, page_delivery=deepcopy(delivery))
        self.draft["articles"].append({"id": page, "title": "新增测试页"})
        self.draft["articles"].sort(key=lambda a: list(PAGE_CHECKS).index(a["id"]))
        entry = deepcopy(old_report["entries"][0])
        entry.update(event_key="new-page-"+str(revision), article_id=page)
        self.draft["entries"].append(entry)
        self.candidate["event_key"] = entry["event_key"]
        self.candidate["topic"] = "other_ai_infra"
        self.audit["decisions"][0]["event_key"] = entry["event_key"]
        for name in PAGE_CHECKS[page]:
            self.audit["checks"][name] = {"status": "checked", "source_ids": ["coverage-"+name], "reason": "真实合成新增检查"}
        for call in self.audit["model_calls"]:
            preceding = call["attempt_records"][-1]
            attempt = dict(preceding, checkpoint_sha256=preceding["result_sha256"],
                           result_sha256=self.artifact(encoded({"round":revision,"stage":call["stage"]})),
                           page_round=dict(revision=revision,page_ids=[page],previous_audit_sha256=sha))
            call["attempt_records"].append(attempt)
            call["attempts"] += 1
            call["elapsed_seconds"] += attempt["elapsed_seconds"]
        self.notice()
        self.previous_summary, self.previous_metadata, self.previous_report = summary, old_metadata, old_report

    def correction_chain(self, withdraw=False):
        evidence=self.audit["page_scope"]["previous_evidence"]
        research=deepcopy(evidence)
        corrected=deepcopy(self.previous_report)
        corrected.update(revision=2,revision_notice={"kind":"correction","summary":"Verified correction"})
        corrected["articles"][0]["title"]="Corrected title"
        corrected["page_delivery"]["previous"]={k:research[k] for k in ("request_id","bundle_sha256")}
        corrected["page_delivery"]["added"]=[]
        if withdraw:
            corrected["entries"] = []
            self.draft["entries"] = [e for e in self.draft["entries"] if e["article_id"] != "model-serving-gpu"]
        raw=encoded(corrected)
        image=b"x"*1_200_082
        bundle=encoded(dict(request_id="morning-20261106-r2",expected_base_sha="e"*40,files=[dict(path="reports/2026-11-06/report.json",sha256=hashlib.sha256(raw).hexdigest(),base64=base64.b64encode(raw).decode()),dict(path="reports/2026-11-06/assets/poster.png",sha256=hashlib.sha256(image).hexdigest(),base64=base64.b64encode(image).decode())]))
        evidence.update(request_id="morning-20261106-r2",bundle_sha256=hashlib.sha256(bundle).hexdigest(),report_commit="d"*40,research_previous=research)
        self.root.joinpath("previous/corrections.json").write_bytes(encoded([dict(bundle_base64=base64.b64encode(bundle).decode(),report_commit="d"*40)]))
        prior={k:evidence[k] for k in ("request_id","bundle_sha256")}
        self.audit["page_scope"]["page_delivery"]["previous"]=prior
        self.draft["page_delivery"]["previous"]=deepcopy(prior)
        self.draft["revision"]=3
        self.draft["articles"][0]["title"]="Corrected title"
        for call in self.audit["model_calls"]:
            call["attempt_records"][-1]["page_round"]["revision"]=3
        return evidence

    def test_correction_chain_retains_research_inputs_and_latest_published_pages(self):
        evidence = self.correction_chain()
        self.check()
        evidence["bundle_sha256"]="f"*64
        self.audit["page_scope"]["page_delivery"]["previous"]["bundle_sha256"]="f"*64
        self.draft["page_delivery"]["previous"]["bundle_sha256"]="f"*64
        self.reject("latest publication fingerprint")

    def test_malformed_correction_chain_cli_is_sanitized(self):
        from ai_brief.cli import main
        from contextlib import redirect_stderr
        from io import StringIO
        self.correction_chain()
        path = self.root / "previous/corrections.json"
        for value in ("PRIVATE_SENTINEL%%%", base64.b64encode(encoded({"private": "PRIVATE_SENTINEL"})).decode(), base64.b64encode(encoded({"files": [{"path": "PRIVATE_SENTINEL/report.json"}]})).decode()):
            with self.subTest(value=value[:20]):
                path.write_bytes(encoded([dict(bundle_base64=value, report_commit="d"*40)]))
                self.save()
                output = StringIO()
                with redirect_stderr(output):
                    self.assertEqual(main(["audit-research", str(self.root)]), 2)
                self.assertIn("Invalid correction chain evidence", output.getvalue())
                self.assertNotIn("PRIVATE_SENTINEL", output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())

    def test_withdrawn_news_cannot_count_for_status_only_completion(self):
        self.correction_chain(withdraw=True)
        # 在纠错页保留状态条目，使其仍是有效页面。
        status = deepcopy(self.previous_report["entries"][0])
        status["event_key"] = "withdrawn-news-status"
        chain_path = self.root / "previous/corrections.json"
        chain = json.loads(chain_path.read_bytes())
        bundle = json.loads(base64.b64decode(chain[0]["bundle_base64"]))
        file = bundle["files"][0]
        corrected = json.loads(base64.b64decode(file["base64"]))
        corrected["entries"] = [status]
        raw = encoded(corrected)
        file.update(base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
        raw = encoded(bundle)
        chain[0]["bundle_base64"] = base64.b64encode(raw).decode()
        chain_path.write_bytes(encoded(chain))
        sha = hashlib.sha256(raw).hexdigest()
        self.audit["page_scope"]["previous_evidence"]["bundle_sha256"] = sha
        self.audit["page_scope"]["page_delivery"]["previous"]["bundle_sha256"] = sha
        self.draft["page_delivery"]["previous"]["bundle_sha256"] = sha
        self.draft["entries"].insert(0, status)
        key = self.draft["entries"][-1]["event_key"]
        self.audit["status_entries"] = [dict(event_key=key, source_ids=["source"], reason="Fully checked, no qualifying news")]
        self.audit["decisions"] = []
        self.candidates["items"] = []
        self.audit["checks"]["hardware"]["status"] = "no_qualifying_items"
        result = self.check()
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("partial_report_requires_verified_items", result["blockers"])

    def test_excluded_rediscovered_old_event_remains_private_evidence(self):
        old_key = self.previous_report["entries"][0]["event_key"]
        candidate = deepcopy(self.candidate)
        candidate["event_key"] = old_key
        self.candidates["items"].append(candidate)
        decision = deepcopy(self.audit["decisions"][0])
        decision.update(event_key=old_key, disposition="out_of_scope", reason="Already covered in the retained page")
        self.audit["decisions"].append(decision)
        result = self.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(self.draft["entries"][0], self.previous_report["entries"][0])
        decision["disposition"] = "selected"
        self.reject("Current candidates cannot redo old page events")

    def test_new_page_cannot_reuse_retained_event_key_to_skip_evidence(self):
        self.draft["entries"][-1].update(event_key=self.previous_report["entries"][0]["event_key"],source_urls=["https://unretained.example/"],update_of="invented")
        self.reject("duplicate event across pages")

    def test_status_only_completion_retains_verified_news_for_partial_gate(self):
        entry=self.draft["entries"][-1]
        key=entry["event_key"]
        self.audit["status_entries"]=[dict(event_key=key,source_ids=["source"],reason="No qualifying new items after full check")]
        self.audit["decisions"]=[]
        self.candidates["items"]=[]
        self.audit["checks"]["hardware"]["status"]="no_qualifying_items"
        result=self.check()
        self.assertEqual(result["readiness"],"partial")
        self.assertEqual(result["selected_count"],self.previous_summary["selected_count"])

    def test_successor_preserves_old_pages_and_cumulative_success_work(self):
        result = self.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["page_scope"]["checked_pages"], ["hardware-status"])
        self.assertEqual(result["budget_accounting"]["successful_seconds"],
                         self.previous_summary["budget_accounting"]["successful_seconds"]+40)
        self.assertEqual(result["budget_accounting"]["successful_attempts"]["model"],4)
        self.assertEqual(self.draft["entries"][0], self.previous_report["entries"][0])

    def test_three_successful_rounds_use_round_count_not_reset_seconds(self):
        self.promote("embodied-training")
        result = self.check()
        self.assertEqual(result["budget_accounting"]["successful_attempts"]["model"],6)
        self.assertEqual(result["readiness"],"partial")

    def test_changed_old_article_entry_and_check_rejected(self):
        self.draft["articles"][0]["title"] = "偷偷修改旧页"
        self.reject("articles must remain")
        self.draft["articles"] = deepcopy(self.previous_report["articles"]) + [{"id":"hardware-status","title":"新增测试页"}]
        self.draft["entries"][0]["source_urls"] = ["https://example.org/changed"]
        self.reject("entries must remain")

    def test_prior_metadata_hash_is_mandatory_and_verified(self):
        summary = deepcopy(self.previous_summary)
        summary.pop("evidence_metadata_sha256")
        raw = encoded(summary)
        self.root.joinpath("previous/audit.json").write_bytes(raw)
        self.audit["page_scope"]["previous_evidence"]["audit_sha256"] = hashlib.sha256(raw).hexdigest()
        self.reject("metadata fingerprint")

    def test_prior_sources_and_model_attempt_prefix_cannot_be_removed(self):
        self.audit["sources"].pop()
        self.reject("prior source")
        self.audit["sources"] = deepcopy(self.previous_metadata["sources"])
        self.audit["model_calls"][0]["attempt_records"][0]["elapsed_seconds"] = 0
        self.reject("preserve model attempts")

    def test_new_round_must_bind_added_revision_and_previous_audit(self):
        attempt = self.audit["model_calls"][0]["attempt_records"][-1]
        original = deepcopy(attempt["page_round"])
        for field, value in [("revision",3),("page_ids",["small-model-status"]),("previous_audit_sha256","d"*64)]:
            attempt["page_round"] = dict(original, **{field:value})
            self.reject("exact current page round")

    def test_new_round_requires_real_new_model_attempts(self):
        self.audit["model_calls"] = deepcopy(self.previous_metadata["model_calls"])
        self.reject("append a real page round")

    def test_old_artifact_missing_still_rejected(self):
        sha = self.previous_metadata["model_calls"][0]["attempt_records"][0]["result_sha256"]
        self.root.joinpath("stage_artifacts",sha+".json").unlink()
        self.reject("Missing audit file")

    def test_successor_cannot_reset_limits_or_start(self):
        self.audit["limits"]["run_seconds"] += 1
        self.reject("reset original budget")
        self.audit["limits"] = deepcopy(self.previous_metadata["limits"])
        self.audit["started_at"] = "2026-11-06T07:21:00+08:00"
        self.reject("reset original budget")

    def test_new_round_does_not_bypass_total_success_budget(self):
        # 保留原额度，累计原轮与新轮实际成功操作耗时。
        call=self.audit["model_calls"][0]
        call["attempt_records"][-1]["elapsed_seconds"]=499
        call["elapsed_seconds"]=519
        self.audit["finished_at"]="2026-11-06T08:30:00+08:00"
        other=self.audit["model_calls"][1]
        other["attempt_records"][-1]["elapsed_seconds"]=499
        other["elapsed_seconds"]=519
        self.assertNotIn("model_budget_exceeded",self.check()["blockers"])
        for index in range(2):
            source=deepcopy(self.audit["sources"][0])
            source.update(id="additional-"+str(index), elapsed_seconds=100)
            source["attempt_records"][0]["elapsed_seconds"]=100
            self.audit["sources"].append(source)
            self.root.joinpath("evidence",source["id"]+".txt").write_bytes(self.content.encode())
        self.assertIn("run_budget_exceeded", self.check()["blockers"])

    def test_current_failure_still_blocks_after_previous_success(self):
        call=self.audit["model_calls"][0]
        call.update(status="failed", error="当前真实阶段失败")
        call["attempt_records"][-1].update(status="failed", error=call["error"])
        self.assertIn("model_stage_failed", self.check()["blockers"])

    def test_old_model_attempt_cannot_be_claimed_as_new_round(self):
        self.audit["model_calls"][0]["attempt_records"][-1].pop("page_round")
        self.reject("exact current page round")

    def test_unknown_previous_attempt_cannot_be_replayed(self):
        call=self.audit["model_calls"][0]
        # 测试模拟此前结果未知的恶意元数据。
        old=self.previous_metadata
        old["model_calls"][0]["attempt_records"][-1].update(status="unknown",error="未知")
        call["attempt_records"][0]=deepcopy(old["model_calls"][0]["attempt_records"][-1])
        raw=encoded(old)
        self.root.joinpath("previous/metadata.json").write_bytes(raw)
        summary=deepcopy(self.previous_summary)
        summary["evidence_metadata_sha256"]=hashlib.sha256(raw).hexdigest()
        summary_bytes=encoded(summary)
        sha=hashlib.sha256(summary_bytes).hexdigest()
        self.root.joinpath("previous/audit.json").write_bytes(summary_bytes)
        self.audit["page_scope"]["previous_evidence"]["audit_sha256"]=sha
        for current in self.audit["model_calls"]:
            current["attempt_records"][-1]["page_round"]["previous_audit_sha256"]=sha
        self.reject("already completed or unknown")

    def test_attempted_two_pages_can_publish_one_without_rewriting_failure(self):
        pages = ["hardware-status", "embodied-training"]
        for call in self.audit["model_calls"]:
            attempt = call["attempt_records"][-1]
            attempt["page_round"]["page_ids"] = pages[:]
            failed = deepcopy(attempt)
            failed.update(status="timeout", error="第二页来源未完成，保留真实尝试", elapsed_seconds=10)
            failed["page_round"]["page_ids"] = ["embodied-training"]
            failed["result_sha256"] = self.artifact(encoded({"stage":call["stage"],"failure":"retained"}))
            attempt["checkpoint_sha256"] = failed["result_sha256"]
            call["attempt_records"].insert(-1, failed)
            call["attempts"] += 1
            call["elapsed_seconds"] += 10
        original = deepcopy(self.audit["model_calls"])
        result = self.check()
        self.assertEqual(result["readiness"], "partial")
        self.assertEqual(result["page_scope"]["page_delivery"]["added"], ["hardware-status"])
        self.assertEqual(result["budget_accounting"]["failed_seconds"], 20)
        self.assertEqual(result["budget_accounting"]["successful_seconds"],
                         self.previous_summary["budget_accounting"]["successful_seconds"]+40)
        self.assertEqual(self.audit["model_calls"], original)
        self.assertIn("embodied-training", [p["id"] for p in result["page_scope"]["page_delivery"]["pending"]])

    def test_attempt_set_must_be_canonical_original_pending(self):
        attempt=self.audit["model_calls"][0]["attempt_records"][-1]
        for pages in (["embodied-training","hardware-status"],["model-serving-gpu"],[],
                      ["hardware-status","hardware-status"]):
            with self.subTest(pages=pages):
                attempt["page_round"]["page_ids"]=pages
                self.reject("current page round|metadata schema")

    def test_failed_attempt_need_not_cover_added_but_last_success_must(self):
        attempt=self.audit["model_calls"][0]["attempt_records"][-1]
        attempt["page_round"]["page_ids"]=["embodied-training"]
        self.reject("Last successful attempt")
