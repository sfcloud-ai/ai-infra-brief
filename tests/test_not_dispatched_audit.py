"""合成的未发起请求证据；不操作生产环境或网络。"""
import hashlib
import json
from datetime import datetime
from copy import deepcopy
from tests import test_community_discovery as discovery_tests
from tests import test_resolved_coverage as resolved_tests
from tests import test_page_successor as successor_tests
from uuid import uuid4
from ai_brief.source_identity import source_identity
import unittest
from tests import test_layered_budget as layered


class NotDispatchedAuditTests(unittest.TestCase):
    setUp = layered.LayeredBudgetTests.setUp
    save = layered.LayeredBudgetTests.save
    check = layered.LayeredBudgetTests.check
    reject = layered.LayeredBudgetTests.reject
    failed = layered.LayeredBudgetTests.failed
    artifact = layered.LayeredBudgetTests.artifact
    add_query = discovery_tests.CommunityDiscoveryTests.add_query
    response = discovery_tests.CommunityDiscoveryTests.response

    def cancellation(self, mutate=None, observation_mutate=None, source=None):
        source = self.audit["sources"][0] if source is None else source
        operation = str(uuid4())
        receipt = f"{datetime.fromisoformat(self.audit['started_at']).timestamp():.6f}"
        request = "morning-" + self.audit["date"].replace("-", "") + "-r1"
        observation = dict(artifact_kind="local_dispatch_observation_not_settlement",
            status="not_dispatched_grant_expired", request_id=request, operation_ids=[operation],
            dispatch_started=False, source_requests=0, command_id=str(uuid4()), receipt_ts=receipt,
            observed_at="2026-11-06T07:21:01+08:00")
        if observation_mutate: observation.update(observation_mutate)
        observation_sha = self.artifact(json.dumps(observation).encode())
        result = dict(schema_version="source_not_dispatched_v1",request_id=request,operation_id=operation,
            url=source["url"], input_sha256=source_identity(source["url"])["hash"],status="not_dispatched",
            dispatch_started=False,source_requests=0,elapsed_seconds=None,reserved_seconds=180,
            observation_sha256=observation_sha,grant_command_id=observation["command_id"],grant_receipt_ts=receipt)
        if mutate: result.update(mutate)
        sha = self.artifact(json.dumps(result).encode())
        previous = dict(status="not_dispatched",elapsed_seconds=None,error="grant expired before dispatch",
            reserved_seconds=180,timing_unavailable_reason="No dispatch; reservation retained without refund",
            operation_id=operation,not_dispatched_evidence_sha256=sha)
        source.update(attempts=2, elapsed_seconds=None,timing_unavailable_reason="Original reservation remains held",
            attempt_records=[previous,dict(source["attempt_records"][0])])
        return source

    def reverse_cancellation(self, source):
        """尾部取消的合成观察不早于保留的真实读取。"""
        source["attempt_records"].reverse()
        attempt = source["attempt_records"][-1]
        result = json.loads((self.root/"stage_artifacts"/(attempt["not_dispatched_evidence_sha256"]+".json")).read_bytes())
        observation = json.loads((self.root/"stage_artifacts"/(result["observation_sha256"]+".json")).read_bytes())
        observation["observed_at"] = source["retrieved_at"]
        result["observation_sha256"] = self.artifact(json.dumps(observation).encode())
        attempt["not_dispatched_evidence_sha256"] = self.artifact(json.dumps(result).encode())

    def test_cancelled_reservation_is_held_not_failed_and_fresh_retry_passes(self):
        baseline=self.check()["budget_accounting"]
        self.cancellation()
        result=self.check(); accounting=result["budget_accounting"]
        self.assertEqual(result["readiness"],"ready")
        self.assertEqual(accounting["failed_attempts"],baseline["failed_attempts"])
        self.assertEqual(accounting["successful_attempts"],baseline["successful_attempts"])
        self.assertEqual(accounting["unknown_attempts"]["source"],1)
        self.assertEqual(accounting["reserved_unknown_seconds"],180)
        self.assertEqual(accounting["charged_seconds"],baseline["charged_seconds"]+180)
        self.assertIn("not_dispatched_reservation_retained",[w["code"] for w in result["warnings"]])

    def test_reject_result_identity_or_started_dispatch(self):
        for change in ({"request_id":"morning-20261107-r1"},{"operation_id":str(uuid4())},
                       {"url":"https://example.org/different"},{"input_sha256":"a"*64},
                       {"dispatch_started":True},{"source_requests":1},{"source_requests":False},
                       {"reserved_seconds":1},{"elapsed_seconds":0}):
            with self.subTest(change=change):
                self.setUp(); self.cancellation(change); self.reject("binding mismatch")

    def test_reject_pre_expiry_or_mismatched_observation(self):
        for change in ({"observed_at":"2026-11-06T07:20:59+08:00"},
                       {"dispatch_started":True},{"source_requests":1},{"operation_ids":[]}):
            with self.subTest(change=change):
                self.setUp(); self.cancellation(observation_mutate=change); self.reject("Not-dispatched")

    def test_evidence_is_required_and_hash_checked(self):
        source=self.cancellation()
        self.root.joinpath("stage_artifacts",source["attempt_records"][0]["not_dispatched_evidence_sha256"]+".json").write_bytes(b"{}")
        self.reject("fingerprint mismatch")

    def test_cancellation_still_consumes_attempt_limit(self):
        self.cancellation(); self.audit["limits"]["source_attempts"]=1
        self.assertIn("source_budget_exceeded",self.check()["blockers"])

    def test_cancellation_does_not_establish_coverage(self):
        source=self.cancellation()
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        self.reject("Candidate needs matching publication, retrieval and source evidence")

    def test_only_source_allows_cancellation(self):
        self.cancellation(); call=self.audit["model_calls"][0]
        call["attempt_records"][0]["status"]="not_dispatched"
        self.reject("schema")

    def test_undispatched_source_cannot_retain_response_bytes(self):
        source=self.failed(kind="incomplete_listing",retained=b"synthetic listing")
        self.cancellation(source=source)
        source.update(status="not_dispatched",attempts=1,attempt_records=source["attempt_records"][:1],retrieved_at=None)
        self.reject("Undispatched source cannot retain response bytes")

    def test_undispatched_source_cannot_support_failed_coverage(self):
        source=self.failed(kind="incomplete_listing")
        self.cancellation(source=source)
        source.update(status="not_dispatched",attempts=1,attempt_records=source["attempt_records"][:1],retrieved_at=None)
        self.reject("Failed coverage requires an attempted matching source")

    def test_undispatched_unchecked_coverage_stays_blocked(self):
        source=next(s for s in self.audit["sources"] if s["id"]=="coverage-qwen_blog")
        self.cancellation(source=source)
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        self.audit["checks"]["qwen_blog"]["status"]="not_checked"
        result=self.check()
        self.assertEqual(result["readiness"],"blocked")
        self.assertIn("required_coverage_not_checked",result["blockers"])

    def pending_discovery(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        discovery_tests.CommunityDiscoveryTests.partial_page(self)
        q=self.add_query("discovery_data_path","GPU communication RDMA transfer in:readme","microsoft/mscclpp")
        source=next(s for s in self.audit["sources"] if s["id"]==q["source_id"])
        self.completed_source=deepcopy(source)
        self.completed_query=deepcopy(q)
        self.cancellation(source=source)
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        q.update(results=[],response_sha256=None)
        self.audit["checks"][q["family"]].update(status="not_checked")
        self.notice()
        return q,source

    def test_pending_cancelled_discovery_can_append_in_successor(self):
        q,source=self.pending_discovery()
        source["published_at"]=None
        self.assertEqual(self.check()["readiness"],"partial")
        successor_tests.PageSuccessorTests.promote(self,"hardware-status")
        self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
        original=deepcopy(source["attempt_records"])
        source.update(status="ok",error="",content_sha256=self.completed_source["content_sha256"],attempts=2,
                      published_at=self.completed_source["published_at"],retrieved_at="2026-11-06T07:31:00+08:00")
        source["attempt_records"].append(self.completed_source["attempt_records"][0])
        next_query=deepcopy(self.completed_query)
        next_query.update(id="resumed-query",attempt=2)
        self.audit["discovery"]["queries"].append(next_query)
        self.audit["checks"][q["family"]].update(status="no_qualifying_items",source_ids=[source["id"]])
        self.notice()
        result=self.check()
        self.assertEqual(result["readiness"],"partial")
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)
        self.assertEqual(source["attempt_records"][:1],original)
        source["attempt_records"][0]["reserved_seconds"]-=1
        self.reject("Successor must preserve all prior source records")

    def test_cancelled_query_cannot_retain_response_after_retry(self):
        q,source=self.pending_discovery()
        self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
        source.update(status="ok",error="",content_sha256=self.completed_source["content_sha256"],attempts=2,retrieved_at="2026-11-06T07:31:00+08:00")
        source["attempt_records"].append(self.completed_source["attempt_records"][0])
        q["response_sha256"]=source["content_sha256"]
        self.reject("Undispatched discovery cannot retain response bytes")

    def test_retained_cancelled_research_cannot_claim_bytes_or_failure(self):
        for keep_bytes in (True,False):
            with self.subTest(keep_bytes=keep_bytes):
                resolved_tests.ResolvedCoverageTests.setUp(self)
                source=next(s for s in self.audit["sources"] if s["id"]=="coverage-qwen_blog")
                self.cancellation(source=source)
                prior=deepcopy(source)
                prior.update(status="not_dispatched",attempts=1,error="not read",
                             attempt_records=source["attempt_records"][:1],retrieved_at=None)
                if not keep_bytes: prior["content_sha256"]=None
                output=json.loads((self.root/"stage_artifacts"/(self.output_sha+".json")).read_bytes())
                output["sources"]=[prior if s["id"]==prior["id"] else s for s in output["sources"]]
                self.original["output_sha256"]=self.artifact(json.dumps(output).encode())
                self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"]=self.artifact(json.dumps(self.original).encode())
                self.reject("Undispatched source cannot retain response bytes" if keep_bytes else "Original failed coverage needs")

    def test_cancelled_auxiliary_is_not_performed(self):
        q,source=self.pending_discovery()
        q["mode"]="backscan"
        self.audit["checks"][q["family"]]["source_ids"]=[]
        self.assertEqual(self.check()["readiness"],"partial")
        self.audit["discovery"]["backscan"]["status"]="performed"
        self.reject("auxiliary work must match its actual queries")

    def test_real_failure_then_cancel_keeps_original_response(self):
        source=self.failed(kind="incomplete_listing",retained=b"synthetic listing")
        source["attempt_records"][0].update(status="failed",error=source["error"])
        self.cancellation(source=source)
        self.reverse_cancellation(source)
        source.update(status="not_dispatched",error="not read")
        result=self.check()
        self.assertEqual(result["readiness"],"partial")
        self.assertEqual(result["budget_accounting"]["failed_attempts"]["source"],1)
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)

    def test_cancelled_retry_preserves_all_real_failure_kinds(self):
        for kind in ("transport","access_denied","unusable_response"):
            with self.subTest(kind=kind):
                self.setUp()
                source=self.failed(kind=kind,retained=b"bad response" if kind=="unusable_response" else None)
                source["attempt_records"][0].update(status="failed",error=source["error"])
                self.cancellation(source=source)
                self.reverse_cancellation(source)
                source.update(status="not_dispatched",error="not read")
                self.assertEqual(self.check()["readiness"],"partial")

    def test_failed_discovery_can_retain_cancelled_sibling(self):
        q,source=self.pending_discovery()
        real=self.add_query(q["family"],"GPU RDMA communication throughput in:readme","microsoft/mscclpp")
        discovery_tests.CommunityDiscoveryTests.fail_query(self,real)
        self.audit["checks"][q["family"]]["source_ids"]=[source["id"],real["source_id"]]
        self.notice()
        self.assertEqual(self.check()["readiness"],"partial")

    def test_incomplete_discovery_keeps_response_after_cancelled_retry(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        discovery_tests.CommunityDiscoveryTests.partial_page(self)
        q=self.add_query("discovery_data_path","GPU RDMA communication in:readme","microsoft/mscclpp")
        discovery_tests.CommunityDiscoveryTests.fail_query(self,q,incomplete=True)
        source=next(s for s in self.audit["sources"] if s["id"]==q["source_id"])
        self.cancellation(source=source)
        self.reverse_cancellation(source)
        source.update(status="not_dispatched",error="not read")
        cancelled=deepcopy(q)
        cancelled.update(id="cancelled-query",attempt=2,response_sha256=None,results=[],truncated=False)
        self.audit["discovery"]["queries"].append(cancelled)
        self.notice()
        result=self.check()
        self.assertEqual(result["readiness"],"partial")
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)

    def add_original_grant(self, source, **changes):
        attempt=source["attempt_records"][0]
        result=json.loads((self.root/"stage_artifacts"/(attempt["not_dispatched_evidence_sha256"]+".json")).read_bytes())
        grant=dict(request_id=result["request_id"],command_id=result["grant_command_id"],dispatch_granted=True,
                   operations=[dict(operation_id=result["operation_id"],input_sha256=result["input_sha256"])],
                   run=dict(lease_until=int(datetime.fromisoformat("2026-11-06T07:20:30+08:00").timestamp()*1000)))
        grant.update(changes)
        raw=b"!ai-research control-receipt "+json.dumps(grant).encode()
        result["grant_receipt_sha256"]=self.artifact(raw)
        attempt["not_dispatched_evidence_sha256"]=self.artifact(json.dumps(result).encode())
        return result["grant_receipt_sha256"]

    def test_original_grant_allows_earlier_lease_expiry_without_refund(self):
        source=self.cancellation(observation_mutate={"observed_at":"2026-11-06T07:20:45+08:00"})
        self.add_original_grant(source)
        result=self.check()
        self.assertEqual(result["readiness"],"ready")
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)

    def test_original_grant_must_bind_identity_and_be_unexpired_original(self):
        for changes in ({"request_id":"morning-20261107-r1"},{"command_id":str(uuid4())},
                        {"operations":[]},{"dispatch_granted":False},{"replayed":True},
                        {"grant_expired":True},{"run":{"lease_until":False}}):
            with self.subTest(changes=changes):
                self.setUp()
                source=self.cancellation()
                self.add_original_grant(source,**changes)
                self.reject("Not-dispatched")

    def test_original_grant_bytes_and_deadline_are_checked(self):
        source=self.cancellation(observation_mutate={"observed_at":"2026-11-06T07:20:20+08:00"})
        sha=self.add_original_grant(source)
        self.reject("must follow grant expiry")
        (self.root/"stage_artifacts"/(sha+".json")).write_bytes(b"changed receipt")
        self.reject("grant receipt fingerprint")

    def test_successful_source_survives_cancelled_reread(self):
        for source_index in (0,2):
            with self.subTest(source_index=source_index):
                self.setUp()
                source=self.cancellation(source=self.audit["sources"][source_index])
                self.reverse_cancellation(source)
                source.update(status="not_dispatched",error="not read")
                result=self.check()
                self.assertEqual(result["readiness"],"ready")
                self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)

    def test_retained_successful_source_survives_cancelled_reread(self):
        resolved_tests.ResolvedCoverageTests.setUp(self)
        source=self.cancellation()
        self.reverse_cancellation(source)
        source.update(status="not_dispatched",error="not read")
        output=json.loads((self.root/"stage_artifacts"/(self.output_sha+".json")).read_bytes())
        output["sources"]=[deepcopy(source) if old["id"]==source["id"] else old for old in output["sources"]]
        self.original["output_sha256"]=self.artifact(json.dumps(output).encode())
        self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"]=self.artifact(json.dumps(self.original).encode())
        self.assertEqual(self.check()["readiness"],"ready")

    def test_selected_discovery_primary_survives_cancelled_reread(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        self.partial_page=lambda: discovery_tests.CommunityDiscoveryTests.partial_page(self)
        discovery_tests.CommunityDiscoveryTests.selected_discovery_page(self)
        source=self.cancellation()
        self.reverse_cancellation(source)
        source.update(status="not_dispatched",error="not read")
        self.assertEqual(self.check()["readiness"],"partial")

    def test_last_executed_discovery_binds_retained_source_bytes(self):
        self.test_incomplete_discovery_keeps_response_after_cancelled_retry()
        q=self.audit["discovery"]["queries"][-1]
        source=next(s for s in self.audit["sources"] if s["id"]==q["source_id"])
        path=self.root/"evidence"/(source["id"]+".txt")
        (self.root/"evidence"/(source["content_sha256"]+".txt")).write_bytes(path.read_bytes())
        path.write_bytes(b"unrelated response")
        source["content_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
        self.reject("Discovery response must bind its actual final attempt")

    def test_discovery_primary_and_baseline_effective_outcome_matrix(self):
        for role in ("primary","baseline"):
            for outcome in ("ok","failed","not_dispatched"):
                with self.subTest(role=role,outcome=outcome):
                    discovery_tests.CommunityDiscoveryTests.setUp(self)
                    self.partial_page=lambda: discovery_tests.CommunityDiscoveryTests.partial_page(self)
                    result=discovery_tests.CommunityDiscoveryTests.selected_discovery_page(self)
                    source=self.audit["sources"][0]
                    if role=="baseline":
                        source=deepcopy(source)
                        source.update(id="baseline-source",url=result["repository_url"]+"/issues/99")
                        self.audit["sources"].append(source)
                        self.root.joinpath("evidence",source["id"]+".txt").write_bytes(self.root.joinpath("evidence/source.txt").read_bytes())
                        result.update(novelty="new_evidence",baseline_url=source["url"])
                        result["source_ids"].append(source["id"])
                        self.audit["decisions"][0]["source_ids"].append(source["id"])
                    self.cancellation(source=source)
                    self.reverse_cancellation(source)
                    source.update(status="not_dispatched",error="not read")
                    if outcome=="failed": source["attempt_records"][0].update(status="failed",error="actual failure")
                    elif outcome=="not_dispatched":
                        source.update(attempts=1,attempt_records=source["attempt_records"][-1:],content_sha256=None,retrieved_at=None)
                    if outcome=="ok": self.assertEqual(self.check()["readiness"],"partial")
                    else: self.reject("project primary evidence" if role=="primary" else "retained previous baseline")

    def test_cancelled_only_suffix_cannot_resolve_original_gap(self):
        resolved_tests.ResolvedCoverageTests.setUp(self)
        source=next(s for s in self.audit["sources"] if s["id"]=="coverage-qwen_blog")
        self.cancellation(source=source)
        self.reverse_cancellation(source)
        source.update(status="not_dispatched",error="not read")
        self.reject("Undispatched suffix must preserve original source response metadata")

    def test_successor_does_not_rewrite_known_publication_metadata(self):
        from ai_brief.discovery import successor_sources_preserved
        source=self.cancellation(source=self.audit["sources"][2])
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        previous=deepcopy(self.audit)
        previous["checks"]={}
        current=deepcopy(previous)
        changed=current["sources"][2]
        changed["attempt_records"].append(dict(status="ok",elapsed_seconds=1,error="",reserved_seconds=180))
        changed.update(status="ok",attempts=2,published_at="2026-11-06T07:00:01+08:00")
        self.assertFalse(successor_sources_preserved(current,previous))
        previous["sources"][2]["published_at"]=None
        self.assertTrue(successor_sources_preserved(current,previous))
        changed["attempt_records"][-1]["status"]="not_dispatched"
        self.assertFalse(successor_sources_preserved(current,previous))

    def test_successful_source_still_requires_consistent_aggregate_error(self):
        self.audit["sources"][0]["error"]="contradictory error"
        self.reject("Successful source needs content fingerprint and no error")

    def test_retained_success_still_requires_consistent_aggregate_error(self):
        resolved_tests.ResolvedCoverageTests.setUp(self)
        output=json.loads((self.root/"stage_artifacts"/(self.output_sha+".json")).read_bytes())
        source=next(s for s in output["sources"] if s["id"]=="coverage-qwen_blog")
        source["error"]="contradictory error"
        self.original["output_sha256"]=self.artifact(json.dumps(output).encode())
        self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"]=self.artifact(json.dumps(self.original).encode())
        self.reject("Original successful source needs retained response bytes")

    def test_auxiliary_cancelled_prefix_can_resume_without_refunding_budget(self):
        for mode in ("backscan", "followup"):
            with self.subTest(mode=mode):
                query, source = self.pending_discovery()
                query["mode"] = mode
                self.audit["checks"][query["family"]]["source_ids"] = []
                second = self.add_query(query["family"], "GPU RDMA throughput in:readme", "microsoft/mscclpp", mode=mode)
                if mode == "followup":
                    lead = self.audit["discovery"]["queries"][0]["results"][0]["url"]
                    query["followup_of"] = second["followup_of"] = lead
                sibling = next(s for s in self.audit["sources"] if s["id"] == second["source_id"])
                self.cancellation(source=sibling)
                sibling.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                               attempt_records=sibling["attempt_records"][:1])
                second.update(results=[],response_sha256=None)
                self.assertEqual(self.check()["readiness"], "partial")
                successor_tests.PageSuccessorTests.promote(self,"hardware-status")
                self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
                original = deepcopy(self.audit["discovery"]["queries"])
                source.update(status="ok",error="",content_sha256=self.completed_source["content_sha256"],attempts=2,retrieved_at="2026-11-06T07:31:00+08:00")
                source["attempt_records"].append(self.completed_source["attempt_records"][0])
                retry = deepcopy(self.completed_query)
                retry.update(id="resumed-auxiliary",mode=mode,attempt=2,followup_of=query["followup_of"])
                self.audit["discovery"]["queries"].append(retry)
                self.audit["discovery"][mode]["status"] = "performed"
                self.add_query(query["family"], "GPU current RDMA evidence in:readme", "microsoft/mscclpp")
                self.notice()
                result = self.check()
                self.assertEqual(result["readiness"], "partial")
                self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],360)
                self.assertEqual(self.audit["discovery"]["queries"][:len(original)], original)
                for index in range(2):
                    added = self.add_query(query["family"], f"GPU transfer benchmark {index}", "microsoft/mscclpp", mode=mode)
                    added["followup_of"] = query["followup_of"]
                self.reject("Auxiliary discovery must remain bounded")

    def test_failed_fixed_check_can_resume_cancelled_sibling_in_successor(self):
        from tests.test_page_scope import PageScopeTests
        self.notice = lambda: PageScopeTests.notice(self)
        PageScopeTests.setUp(self)
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-hardware")
        successful = deepcopy(source)
        sibling = deepcopy(source)
        sibling.update(id="hardware-failed",url="https://developer.nvidia.com/blog/other/",status="failed",
                       error="transport failure",content_sha256=None)
        sibling["attempt_records"][0].update(status="failed",error="transport failure")
        self.audit["sources"].append(sibling)
        self.cancellation(source=source)
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        self.audit["checks"]["hardware"].update(status="failed",failure_kind="transport",source_ids=[sibling["id"],source["id"]])
        self.notice()
        self.assertEqual(self.check()["readiness"],"partial")
        original = deepcopy(source["attempt_records"])
        successor_tests.PageSuccessorTests.promote(self,"hardware-status")
        self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
        source.update(status="ok",error="",attempts=2,content_sha256=successful["content_sha256"],retrieved_at="2026-11-06T07:31:00+08:00")
        source["attempt_records"].append(successful["attempt_records"][0])
        result = self.check()
        self.assertEqual(result["readiness"],"partial")
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180)
        self.assertEqual(source["attempt_records"][:1],original)

    def test_completed_check_still_protects_cancelled_reference(self):
        from ai_brief.discovery import successor_sources_preserved
        source = self.cancellation()
        source.update(status="not_dispatched",attempts=1,error="not read",content_sha256=None,retrieved_at=None,
                      attempt_records=source["attempt_records"][:1])
        previous = deepcopy(self.audit)
        previous["checks"] = {"hardware": dict(status="checked",source_ids=[source["id"]])}
        current = deepcopy(previous)
        changed = current["sources"][0]
        changed["attempt_records"].append(dict(status="ok",elapsed_seconds=1,error="",reserved_seconds=180))
        changed.update(status="ok",attempts=2)
        for status in ("checked","no_qualifying_items","failed","not_checked"):
            with self.subTest(status=status):
                previous["checks"]["hardware"]["status"] = status
                self.assertEqual(successor_sources_preserved(current,previous),status in {"failed","not_checked"})

    def test_cancelled_suffix_preserves_all_non_accounting_fields(self):
        from ai_brief.discovery import cancelled_suffix_preserves_response
        for status in ("ok", "failed", "not_dispatched"):
            self.setUp()
            source = self.cancellation()
            self.reverse_cancellation(source)
            source.update(status="not_dispatched",error="not read")
            source["attempt_records"][0]["status"] = status
            previous = deepcopy(source)
            previous["attempt_records"] = previous["attempt_records"][:1]
            previous.update(attempts=1,status=status)
            self.assertTrue(cancelled_suffix_preserves_response(source, previous))
            for field in ("id", "url", "content_sha256", "published_at", "retrieved_at", "future_field"):
                with self.subTest(status=status,field=field):
                    changed = deepcopy(source)
                    changed[field] = "changed"
                    self.assertFalse(cancelled_suffix_preserves_response(changed, previous))
            source["attempt_records"][-1]["status"] = "ok"
            source["content_sha256"] = "a"*64
            self.assertTrue(cancelled_suffix_preserves_response(source, previous))

    def test_resolution_cancelled_suffix_cannot_change_response_metadata(self):
        for field in ("content_sha256", "retrieved_at", "published_at"):
            with self.subTest(field=field):
                resolved_tests.ResolvedCoverageTests.setUp(self)
                source = next(s for s in self.audit["sources"] if s["id"] == "coverage-glm_blog")
                self.cancellation(source=source)
                self.reverse_cancellation(source)
                source.update(status="not_dispatched",error="not read")
                self.assertEqual(self.check()["readiness"],"ready")
                if field == "content_sha256":
                    path = self.root/"evidence"/(source["id"]+".txt")
                    (self.root/"evidence"/(source[field]+".txt")).write_bytes(path.read_bytes())
                    path.write_bytes(b"Unfetched replacement evidence")
                    source[field] = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    source[field] = "2026-11-05T07:25:00+08:00" if field == "published_at" else "2026-11-06T07:25:00+08:00"
                if field == "retrieved_at":
                    self.set_observation_time(source["attempt_records"][-1], source[field])
                self.reject("Undispatched suffix must preserve original source response metadata")

    def test_successor_cancelled_suffix_cannot_rewrite_failed_response(self):
        from ai_brief.discovery import successor_sources_preserved
        source = self.cancellation()
        self.reverse_cancellation(source)
        source["attempt_records"][0].update(status="failed",error="actual failure")
        source.update(status="not_dispatched",error="not read")
        previous = deepcopy(self.audit)
        old = previous["sources"][0]
        old.update(status="failed",error="actual failure",attempts=1,attempt_records=old["attempt_records"][:1])
        previous["checks"] = {"discovery_memory": dict(status="failed",source_ids=[source["id"]])}
        previous["discovery"] = dict(queries=[dict(family="discovery_memory",source_id=source["id"])])
        self.assertTrue(successor_sources_preserved(self.audit,previous))
        for field in ("content_sha256", "published_at", "retrieved_at"):
            with self.subTest(field=field):
                changed = deepcopy(self.audit)
                changed["sources"][0][field] = "changed"
                self.assertFalse(successor_sources_preserved(changed,previous))

    def test_retry_cannot_precede_its_no_dispatch_observation(self):
        self.cancellation(observation_mutate={"observed_at":"2026-11-06T07:29:59+08:00"})
        self.reject("Source retry retrieval must follow its no-dispatch observation")

    def test_equal_precision_retry_time_is_allowed(self):
        source = self.audit["sources"][0]
        self.cancellation(observation_mutate={"observed_at":source["retrieved_at"]})
        self.assertEqual(self.check()["readiness"],"ready")

    def test_result_reservation_cannot_use_boolean_as_one_second(self):
        source = self.cancellation(mutate={"reserved_seconds":True})
        source["attempt_records"][0]["reserved_seconds"] = 1
        self.reject("binding mismatch")

    def test_executed_discovery_failure_cannot_hide_behind_cancelled_query(self):
        for outcome in ("failed","timeout"):
            with self.subTest(outcome=outcome):
                q,source = self.pending_discovery()
                original = deepcopy(source["attempt_records"][0])
                failed = dict(status=outcome,elapsed_seconds=1,error="actual request failure",reserved_seconds=180)
                source.update(attempts=2,attempt_records=[failed,original],retrieved_at=self.completed_source["retrieved_at"])
                q["attempt"] = 2
                self.set_observation_time(original, source["retrieved_at"])
                self.reject("Every retained discovery attempt needs its original query and response")

    def test_noncanonical_search_url_uses_issuer_identity(self):
        q,source = self.pending_discovery()
        attempt = source["attempt_records"][0]
        path = self.root/"stage_artifacts"/(attempt["not_dispatched_evidence_sha256"]+".json")
        result = json.loads(path.read_bytes())
        self.assertEqual(result["input_sha256"],source_identity(source["url"])["hash"])
        raw_hash = hashlib.sha256(json.dumps({"method":"GET","url":source["url"]},separators=(",", ":"),ensure_ascii=False).encode()).hexdigest()
        self.assertNotEqual(raw_hash,result["input_sha256"])
        self.assertEqual(self.check()["readiness"],"partial")
        result["input_sha256"] = raw_hash
        attempt["not_dispatched_evidence_sha256"] = self.artifact(json.dumps(result).encode())
        self.reject("binding mismatch")

    def set_observation_time(self, attempt, timestamp):
        """修改合成原观察并重新绑定所有内容哈希。"""
        result = json.loads((self.root/"stage_artifacts"/(attempt["not_dispatched_evidence_sha256"]+".json")).read_bytes())
        observation = json.loads((self.root/"stage_artifacts"/(result["observation_sha256"]+".json")).read_bytes())
        observation["observed_at"] = timestamp
        result["observation_sha256"] = self.artifact(json.dumps(observation).encode())
        attempt["not_dispatched_evidence_sha256"] = self.artifact(json.dumps(result).encode())

    def test_unread_source_may_have_no_retrieval_but_executed_source_must(self):
        q,source = self.pending_discovery()
        source["retrieved_at"] = None
        self.assertEqual(self.check()["readiness"],"partial")
        self.setUp()
        self.audit["sources"][0]["retrieved_at"] = None
        self.reject("Executed source requires a retrieval timestamp")

    def test_cancelled_discovery_query_cannot_be_omitted(self):
        self.test_incomplete_discovery_keeps_response_after_cancelled_retry()
        self.audit["discovery"]["queries"].pop()
        self.reject("Every retained discovery attempt")

    def test_observation_cannot_predate_receipt_even_if_lease_expired(self):
        receipt = str(datetime.fromisoformat("2026-11-06T07:21:00+08:00").timestamp())
        receipt = f"{float(receipt):.6f}"
        source = self.cancellation(mutate={"grant_receipt_ts":receipt},observation_mutate={"receipt_ts":receipt,"observed_at":"2026-11-06T07:20:45+08:00"})
        self.add_original_grant(source)
        self.reject("observation must follow grant expiry")

    def test_original_research_cannot_contain_a_later_cancel_observation(self):
        self.test_retained_successful_source_survives_cancelled_reread()
        source = self.audit["sources"][0]
        self.set_observation_time(source["attempt_records"][-1],"2026-11-06T07:24:00+08:00")
        output = json.loads((self.root/"stage_artifacts"/(self.original["output_sha256"]+".json")).read_bytes())
        output["sources"] = [deepcopy(source) if old["id"] == source["id"] else old for old in output["sources"]]
        self.original["output_sha256"] = self.artifact(json.dumps(output).encode())
        self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"] = self.artifact(json.dumps(self.original).encode())
        self.reject("Original research cannot finish before its retained no-dispatch observations")

    def test_source_order_respects_recorded_precision_and_all_cancel_positions(self):
        cases = [
            (("ok","cancel","ok"), ("07:24:00",), "07:25:00", None),
            (("cancel","cancel","ok"), ("07:21:01","07:22:00"), "07:23:00", None),
            (("cancel","cancel","ok"), ("07:22:01","07:22:00"), "07:23:00", "preserve attempt order"),
            (("cancel","ok"), ("07:22:00.500",), "07:22:00", None),
            (("cancel","ok"), ("07:22:00.1234567890123456789",), "07:22:00.1234567890123456789", None),
            (("cancel","ok"), ("07:22:00.1234567890123456789",), "07:22:00.1234567890123456788", "retrieval must follow"),
            (("cancel","ok"), ("07:22:01",), "07:22:00", "retrieval must follow"),
            (("ok","cancel"), ("07:22:00",), "07:22:00.500", None),
            (("ok","cancel"), ("07:21:59",), "07:22:00", "must follow the retained read"),
            (("cancel","cancel"), ("07:21:01","07:22:00"), None, None),
        ]
        for statuses,observations,retrieved,error in cases:
            with self.subTest(statuses=statuses,observations=observations):
                q,source = self.pending_discovery()
                attempts,queries = [],[]
                observed = iter(observations)
                for index,status in enumerate(statuses,1):
                    query = deepcopy(self.completed_query)
                    query.update(id=f"ordered-query-{index}",attempt=index)
                    if status == "cancel":
                        attempt = self.cancellation(source=deepcopy(self.completed_source))["attempt_records"][0]
                        self.set_observation_time(attempt,"2026-11-06T"+next(observed)+"+08:00")
                        query.update(response_sha256=None,results=[])
                    else:
                        attempt = deepcopy(self.completed_source["attempt_records"][0])
                    attempts.append(attempt);queries.append(query)
                source.update(attempt_records=attempts,attempts=len(attempts),status=attempts[-1]["status"],error=attempts[-1]["error"],
                              content_sha256=self.completed_source["content_sha256"] if "ok" in statuses else None,
                              retrieved_at="2026-11-06T"+retrieved+"+08:00" if retrieved else None)
                self.audit["discovery"]["queries"][-1:] = queries
                self.audit["checks"][q["family"]].update(status="no_qualifying_items" if "ok" in statuses else "not_checked")
                self.notice()
                if error: self.reject(error)
                else:
                    result = self.check()
                    self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],180*statuses.count("cancel"))

    def test_frozen_non_cancel_retry_retains_legacy_query_contract(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        q = self.audit["discovery"]["queries"][0]
        source = next(s for s in self.audit["sources"] if s["id"] == q["source_id"])
        source["attempt_records"].insert(0, dict(status="timeout", elapsed_seconds=1, error="original timeout", reserved_seconds=180))
        source.update(attempts=2, elapsed_seconds=source["elapsed_seconds"]+1)
        q["attempt"] = 2
        self.assertEqual(self.check()["readiness"], "ready")

    def test_successor_cancel_does_not_reconstruct_accepted_legacy_queries(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        discovery_tests.CommunityDiscoveryTests.partial_page(self)
        query = self.add_query("discovery_data_path", "GPU legacy RDMA transfer in:readme", "microsoft/mscclpp")
        discovery_tests.CommunityDiscoveryTests.fail_query(self, query)
        source = next(s for s in self.audit["sources"] if s["id"] == query["source_id"])
        source["attempt_records"].insert(0, dict(status="timeout", elapsed_seconds=1,
                                               error="original timeout", reserved_seconds=180))
        source.update(attempts=2, elapsed_seconds=source["elapsed_seconds"] + 1)
        query["attempt"] = 2
        self.notice()
        self.assertEqual(self.check()["readiness"], "partial")
        successor_tests.PageSuccessorTests.promote(self, "hardware-status")
        current = self.add_query("discovery_data_path", "GPU current RDMA transfer in:readme", "microsoft/mscclpp")
        self.audit["checks"]["discovery_data_path"]["source_ids"] = [source["id"], current["source_id"]]
        self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
        cancellation = self.cancellation(source=deepcopy(source))["attempt_records"][0]
        self.set_observation_time(cancellation, "2026-11-06T07:31:00+08:00")
        source["attempt_records"].append(cancellation)
        source.update(attempts=3, elapsed_seconds=None, status="not_dispatched",
                      error="grant expired before dispatch", timing_unavailable_reason="Reservation retained")
        added = dict(deepcopy(query), id="successor-cancel", attempt=3,
                     response_sha256=None, results=[], truncated=False)
        self.audit["discovery"]["queries"].append(added)
        self.notice()
        result = self.check()
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"], 180)
        old = next(s for s in self.previous_metadata["sources"] if s["id"] == source["id"])
        self.assertEqual(source["attempt_records"][:2], old["attempt_records"])
        self.audit["discovery"]["queries"].remove(added)
        self.reject("Every retained discovery attempt")

    def test_unread_source_cannot_claim_a_retrieval(self):
        q, source = self.pending_discovery()
        source["retrieved_at"] = self.completed_source["retrieved_at"]
        self.reject("Undispatched source cannot claim a retrieval timestamp")

    def test_resumed_cancelled_source_must_belong_to_new_page_round(self):
        self.test_pending_cancelled_discovery_can_append_in_successor()
        source = next(s for s in self.audit["sources"] if s["id"] == self.completed_source["id"])
        source["attempt_records"][0]["reserved_seconds"] += 1
        source["retrieved_at"] = self.completed_source["retrieved_at"]
        self.reject("Resumed source read must follow the previous audit finish")

    def test_observation_fractional_precision_cannot_hide_a_reverse_read(self):
        for separator in ("T", "t", " "):
            with self.subTest(separator=separator):
                self.setUp()
                source = self.cancellation(observation_mutate={"observed_at": "2026-11-06"+separator+"07:22:00.800+08:00"})
                source["retrieved_at"] = "2026-11-06T07:22:00.100+08:00"
                self.reject("Source retry retrieval must follow")

    def test_original_snapshot_checks_its_own_tail_cancel_order(self):
        self.test_retained_successful_source_survives_cancelled_reread()
        source = self.audit["sources"][0]
        self.set_observation_time(source["attempt_records"][-1], "2026-11-06T07:21:01+08:00")
        output = json.loads((self.root/"stage_artifacts"/(self.original["output_sha256"]+".json")).read_bytes())
        output["sources"] = [deepcopy(source) if old["id"] == source["id"] else old for old in output["sources"]]
        self.original["output_sha256"] = self.artifact(json.dumps(output).encode())
        self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"] = self.artifact(json.dumps(self.original).encode())
        source["attempt_records"].append(dict(status="ok",elapsed_seconds=1,error="",reserved_seconds=180))
        source.update(attempts=3,status="ok",error="",retrieved_at="2026-11-06T07:24:00+08:00")
        self.reject("Trailing no-dispatch observation must follow the retained read")

    def test_previous_page_unread_snapshot_cannot_claim_a_retrieval(self):
        self.test_pending_cancelled_discovery_can_append_in_successor()
        source = next(s for s in self.audit["sources"] if s["id"] == self.completed_source["id"])
        source["attempt_records"][0]["reserved_seconds"] += 1
        prior = self.root/"previous"
        metadata = json.loads((prior/"metadata.json").read_bytes())
        old = next(s for s in metadata["sources"] if s["id"] == source["id"])
        old["retrieved_at"] = self.completed_source["retrieved_at"]
        raw = json.dumps(metadata).encode()
        (prior/"metadata.json").write_bytes(raw)
        summary = json.loads((prior/"audit.json").read_bytes())
        summary["evidence_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
        raw = json.dumps(summary).encode()
        (prior/"audit.json").write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.audit["page_scope"]["previous_evidence"]["audit_sha256"] = digest
        for call in self.audit["model_calls"]:
            call["attempt_records"][-1]["page_round"]["previous_audit_sha256"] = digest
        self.reject("Undispatched source cannot claim a retrieval timestamp")

    def test_malformed_observation_timestamp_is_a_validation_error(self):
        for value in (None, 0, 1.25, [], {}):
            with self.subTest(value=value):
                self.setUp()
                self.cancellation(observation_mutate={"observed_at": value})
                self.reject("Invalid audit timestamp")

    def test_submicrosecond_observation_cannot_cross_run_finish(self):
        q,source = self.pending_discovery()
        self.audit["finished_at"] = "2026-11-06T07:30:00.000000000+08:00"
        self.set_observation_time(source["attempt_records"][0], "2026-11-06T07:30:00.000000001+08:00")
        self.reject("observation must follow grant expiry")

    def test_appended_cancel_cannot_precede_original_snapshot_read(self):
        resolved_tests.ResolvedCoverageTests.setUp(self)
        source = self.cancellation()
        self.reverse_cancellation(source)
        self.set_observation_time(source["attempt_records"][-1], "2026-11-06T07:21:01+08:00")
        source["attempt_records"].append(dict(status="ok",elapsed_seconds=1,error="",reserved_seconds=180))
        source.update(attempts=3,status="ok",error="",retrieved_at="2026-11-06T07:24:00+08:00")
        self.reject("Appended cancellation must follow the original retained read")

    def test_cancelled_search_cannot_drop_its_entire_query_history(self):
        q,source = self.pending_discovery()
        source["attempt_records"].insert(0,dict(status="timeout",elapsed_seconds=1,error="original timeout",reserved_seconds=180))
        source.update(attempts=2,retrieved_at=self.completed_source["retrieved_at"])
        self.set_observation_time(source["attempt_records"][-1], source["retrieved_at"])
        self.audit["discovery"]["queries"].remove(q)
        self.audit["checks"][q["family"]]["source_ids"] = []
        self.reject("Every retained discovery attempt")

    def test_submicrosecond_observation_cannot_cross_original_finish(self):
        self.test_retained_successful_source_survives_cancelled_reread()
        source = self.audit["sources"][0]
        self.set_observation_time(source["attempt_records"][-1], "2026-11-06T07:23:00.000000001+08:00")
        output = json.loads((self.root/"stage_artifacts"/(self.original["output_sha256"]+".json")).read_bytes())
        output["sources"] = [deepcopy(source) if old["id"] == source["id"] else old for old in output["sources"]]
        self.original["output_sha256"] = self.artifact(json.dumps(output).encode())
        self.original["finished_at"] = "2026-11-06T07:23:00.000000000+08:00"
        self.audit["model_calls"][0]["attempt_records"][0]["result_sha256"] = self.artifact(json.dumps(self.original).encode())
        self.reject("Original research cannot finish before its retained no-dispatch observations")

    def test_submicrosecond_observation_cannot_cross_previous_finish(self):
        self.test_pending_cancelled_discovery_can_append_in_successor()
        source = next(s for s in self.audit["sources"] if s["id"] == self.completed_source["id"])
        source["attempt_records"][0]["reserved_seconds"] += 1
        self.set_observation_time(source["attempt_records"][0], "2026-11-06T07:30:00.000000001+08:00")
        prior = self.root/"previous"
        metadata = json.loads((prior/"metadata.json").read_bytes())
        metadata["finished_at"] = "2026-11-06T07:30:00.000000000+08:00"
        old = next(s for s in metadata["sources"] if s["id"] == source["id"])
        old["attempt_records"][0] = deepcopy(source["attempt_records"][0])
        raw = json.dumps(metadata).encode()
        (prior/"metadata.json").write_bytes(raw)
        summary = json.loads((prior/"audit.json").read_bytes())
        summary["evidence_metadata_sha256"] = hashlib.sha256(raw).hexdigest()
        raw = json.dumps(summary).encode()
        (prior/"audit.json").write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.audit["page_scope"]["previous_evidence"]["audit_sha256"] = digest
        for call in self.audit["model_calls"]:
            call["attempt_records"][-1]["page_round"]["previous_audit_sha256"] = digest
        self.reject("Previous audit cannot finish before its retained no-dispatch observations")

    def test_fixed_repository_search_with_cancel_is_not_discovery(self):
        for limit in (10, 100):
            with self.subTest(limit=limit):
                discovery_tests.CommunityDiscoveryTests.setUp(self)
                source = next(s for s in self.audit["sources"] if s["id"] == "coverage-vllm")
                source["url"] = f"https://api.github.com/search/issues?q=repo%3Avllm-project%2Fvllm%20is%3Apr&per_page={limit}"
                self.assertEqual(self.check()["readiness"], "ready")
                self.cancellation(source=source)
                self.assertEqual(self.check()["readiness"], "ready")

    def test_topic_repository_search_with_cancel_is_not_discovery(self):
        from urllib.parse import urlencode
        for topic, repository in (("embodied_training", "Physical-Intelligence/openpi"),
                                  ("embodied_training", "NVIDIA/Isaac-GR00T"),
                                  ("embodied_training", "huggingface/lerobot"),
                                  ("model_serving", "LMCache/LMCache"),
                                  ("model_serving", "NVIDIA/TensorRT-LLM")):
            with self.subTest(topic=topic, repository=repository):
                discovery_tests.CommunityDiscoveryTests.setUp(self)
                old_id = self.audit["checks"][topic]["source_ids"][0]
                source = deepcopy(next(s for s in self.audit["sources"] if s["id"] == old_id))
                source.update(id="fixed-topic-search", url="https://api.github.com/search/issues?" +
                              urlencode(dict(q="repo:" + repository + " is:pr", per_page=10)))
                (self.root/"evidence"/(source["id"]+".txt")).write_bytes((self.root/"evidence"/(old_id+".txt")).read_bytes())
                self.audit["sources"].append(source)
                self.audit["checks"][topic]["source_ids"] = [source["id"]]
                self.assertEqual(self.check()["readiness"], "ready")
                self.cancellation(source=source)
                self.assertEqual(self.check()["readiness"], "ready")

    def test_cancellation_preserves_equivalent_original_url_strings(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        source = next(s for s in self.audit["sources"] if s["id"] == "coverage-vllm")
        source["url"] += "?b=2&a=1"
        original = source["url"]
        equivalent = original.split("?", 1)[0] + "?a=1&b=2"
        self.cancellation(source=source, mutate={"url": equivalent})
        self.assertEqual(self.check()["readiness"], "ready")
        self.assertEqual(source["url"], original)
        attempt = source["attempt_records"][0]
        result = json.loads((self.root/"stage_artifacts"/(attempt["not_dispatched_evidence_sha256"]+".json")).read_bytes())
        self.assertEqual(result["url"], equivalent)

    def test_cancellation_url_identity_change_is_rejected(self):
        for query in ("a=1&b=3", "a=1&b=2&b=2"):
            with self.subTest(query=query):
                discovery_tests.CommunityDiscoveryTests.setUp(self)
                source = next(s for s in self.audit["sources"] if s["id"] == "coverage-vllm")
                base = source["url"]
                source["url"] += "?b=2&a=1"
                self.cancellation(source=source, mutate={"url": base + "?" + query})
                self.reject("Not-dispatched evidence binding mismatch")

    def test_targeted_followup_cancellation_cannot_erase_query_history(self):
        discovery_tests.CommunityDiscoveryTests.setUp(self)
        query = self.add_query("discovery_compute", "repo:JustAResearcher/gpu-unlock-ada benchmark",
                               "JustAResearcher/gpu-unlock-ada", mode="followup")
        query["followup_of"] = self.audit["discovery"]["queries"][2]["results"][0]["url"]
        source = next(s for s in self.audit["sources"] if s["id"] == query["source_id"])
        source.update(status="timeout", error="transport timeout", content_sha256=None)
        source["attempt_records"][0].update(status="timeout", error="transport timeout")
        query.update(response_sha256=None, results=[], truncated=False)
        self.cancellation(source=source)
        self.reverse_cancellation(source)
        source.update(status="not_dispatched", error="grant expired before dispatch")
        cancelled = dict(deepcopy(query), id="followup-cancel", attempt=2)
        self.audit["discovery"]["queries"].append(cancelled)
        self.audit["discovery"]["followup"]["status"] = "performed"
        self.assertEqual(self.check()["readiness"], "ready")
        self.audit["discovery"]["queries"].remove(query)
        self.audit["discovery"]["queries"].remove(cancelled)
        self.audit["discovery"]["followup"]["status"] = "none_due"
        self.reject("Every retained discovery attempt")

    def test_invalid_observation_timezone_offset_is_rejected(self):
        for offset in ("+07:60", "+08:99", "+24:00"):
            with self.subTest(offset=offset):
                self.setUp()
                self.cancellation(observation_mutate={"observed_at":"2026-11-06T07:21:01"+offset})
                self.reject("Invalid audit timestamp|Unsupported observation timestamp precision")

    def test_new_successor_cancellations_must_follow_previous_finish(self):
        for new_source in (False, True):
            with self.subTest(new_source=new_source):
                q,source = self.pending_discovery()
                successor_tests.PageSuccessorTests.promote(self,"hardware-status")
                self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
                attempt = self.cancellation(source=deepcopy(self.completed_source),observation_mutate={"observed_at":"2026-11-06T07:22:00+08:00"})["attempt_records"][0]
                if new_source:
                    source = deepcopy(source)
                    source.update(id="new-cancelled-source",url="https://api.github.com/search/repositories?q=GPU+new+transfer+in%3Areadme&per_page=10&page=1")
                    attempt = self.cancellation(source=source,observation_mutate={"observed_at":"2026-11-06T07:22:00+08:00"})["attempt_records"][0]
                    source.update(attempt_records=[attempt],attempts=1)
                    self.audit["sources"].append(source)
                else:
                    source["attempt_records"].append(attempt)
                    source["attempts"] = 2
                appended = deepcopy(q)
                appended.update(id="new-reservation",source_id=source["id"],attempt=source["attempts"])
                if new_source: appended["query"] = "GPU new transfer in:readme"
                self.audit["discovery"]["queries"].append(appended)
                real = self.add_query(q["family"],"GPU transfer interconnect in:readme","microsoft/mscclpp")
                self.audit["checks"][q["family"]]["source_ids"] = list(dict.fromkeys([q["source_id"],source["id"],real["source_id"]]))
                self.notice()
                self.reject("Appended cancellation must follow the previous audit finish")
                self.set_observation_time(attempt,"2026-11-06T07:31:00+08:00")
                self.assertEqual(self.check()["readiness"], "partial")

    def test_bing_lookup_only_requires_discovery_for_bounded_github_shape(self):
        cases = [
            ("https://www.bing.com/search?q=GPU+release&format=rss", False),
            ("https://www.bing.com/search?q=GPU+release&format=rss&count=10", False),
            ("https://www.bing.com/search?q=GPU+release+site%3Agithub.com&format=rss&count=10", True),
            ("https://api.github.com/search/repositories?q=GPU+release&per_page=100&page=1", False),
        ]
        for url,discovery in cases:
            with self.subTest(url=url):
                discovery_tests.CommunityDiscoveryTests.setUp(self)
                source = deepcopy(self.audit["sources"][0])
                source.update(id="ancillary-search",url=url)
                self.cancellation(source=source)
                source.update(status="not_dispatched",error="not read",attempts=1,
                              attempt_records=source["attempt_records"][:1],content_sha256=None,retrieved_at=None,published_at=None)
                self.audit["sources"].append(source)
                if discovery: self.reject("Every retained discovery attempt")
                else: self.assertEqual(self.check()["readiness"], "ready")

    def test_failed_cancelled_source_retry_belongs_to_successor_round(self):
        q,source = self.pending_discovery()
        source["attempt_records"].insert(0,dict(status="timeout",elapsed_seconds=1,error="actual timeout",reserved_seconds=180))
        source.update(attempts=2,retrieved_at=self.completed_source["retrieved_at"])
        self.set_observation_time(source["attempt_records"][-1],"2026-11-06T07:23:00+08:00")
        failed = deepcopy(q)
        failed.update(id="failed-before-cancel",attempt=1)
        q["attempt"] = 2
        self.audit["discovery"]["queries"].insert(self.audit["discovery"]["queries"].index(q),failed)
        self.audit["checks"][q["family"]].update(status="failed",failure_kind="transport")
        self.notice()
        self.assertEqual(self.check()["readiness"], "partial")
        successor_tests.PageSuccessorTests.promote(self,"hardware-status")
        self.audit["finished_at"] = "2026-11-06T07:32:00+08:00"
        source["attempt_records"].append(deepcopy(self.completed_source["attempt_records"][0]))
        source.update(attempts=3,status="ok",error="",content_sha256=self.completed_source["content_sha256"],retrieved_at="2026-11-06T07:24:00+08:00")
        retry = deepcopy(self.completed_query)
        retry.update(id="mixed-prefix-retry",attempt=3)
        self.audit["discovery"]["queries"].append(retry)
        self.audit["checks"][q["family"]] = dict(status="no_qualifying_items",source_ids=[source["id"]],reason="Synthetic complete retry")
        self.notice()
        self.reject("Resumed source read must follow the previous audit finish")
        source["retrieved_at"] = "2026-11-06T07:31:00+08:00"
        self.assertEqual(self.check()["readiness"], "partial")

    def test_first_cancel_after_retry_belongs_to_successor_round(self):
        q,s=self.pending_discovery()
        cancel=deepcopy(s['attempt_records'][0])
        s.update(status='timeout',error='actual timeout',retrieved_at=self.completed_source['retrieved_at'],attempt_records=[dict(status='timeout',elapsed_seconds=1,error='actual timeout',reserved_seconds=180)],elapsed_seconds=1)
        self.audit['checks'][q['family']].update(status='failed',failure_kind='transport')
        s.pop('timing_unavailable_reason',None)
        self.notice()
        self.assertEqual(self.check()['readiness'], 'partial')
        successor_tests.PageSuccessorTests.promote(self,'hardware-status')
        self.audit['finished_at']='2026-11-06T07:32:00+08:00'
        s['attempt_records'] += [deepcopy(self.completed_source['attempt_records'][0]),cancel]
        s.update(attempts=3,status='not_dispatched',error='not read',elapsed_seconds=None,timing_unavailable_reason='reservation held',content_sha256=self.completed_source['content_sha256'],retrieved_at='2026-11-06T07:24:00+08:00')
        self.set_observation_time(cancel,'2026-11-06T07:31:30+08:00')
        retry=deepcopy(self.completed_query); retry.update(id='success-after-timeout',attempt=2)
        cq=deepcopy(q); cq.update(id='cancel-after-success',attempt=3)
        self.audit['discovery']['queries'] += [retry,cq]
        self.audit['checks'][q['family']]=dict(status='no_qualifying_items',source_ids=[s['id']],reason='Synthetic completed retry')
        self.notice()
        self.reject('Resumed source read must follow the previous audit finish')
        s['retrieved_at']='2026-11-06T07:31:00+08:00'
        self.assertEqual(self.check()['readiness'], 'partial')
