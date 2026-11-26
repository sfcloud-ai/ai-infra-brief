"""首发前模型增量续接的离线合同回归，全部证据均为合成数据。"""

import hashlib
import json
from uuid import uuid4

from tests.test_layered_budget import LayeredBudgetTests
from tests import test_page_successor as successor_tests
from tests.test_resolved_coverage import ResolvedCoverageTests
from tests.test_research_audit import encoded
from ai_brief.research_audit import _json, _require
from ai_brief.research_continuation import referenced_editor_input, validate_continuation, validate_current_outputs
from ai_brief.storage import BriefError
from ai_brief.source_identity import source_identity
from copy import deepcopy


class ModelContinuationAuditTests(LayeredBudgetTests):
    notice = successor_tests.PageSuccessorTests.notice
    def test_original_editor_findings_are_bound_and_not_modified(self):
        request = "morning-20261101-r1"
        editor_id, research_id, source_id = map(str, (uuid4(),uuid4(),uuid4()))
        common = dict(request_id=request, operation_id=research_id, input_sha256="a"*64, stage="research")
        row = dict(operation_id=source_id,input_sha256="b"*64,result_sha256="c"*64,body_sha256="d"*64)
        findings = self.js(dict(**common,source_records={"prior":row}))
        outputs = [dict(name="findings.json", sha256=findings)]
        research = dict(operation_id=research_id,input_sha256="a"*64,status="partial",elapsed_seconds=1,outputs=outputs)
        result = self.js(dict(**common,status="partial",elapsed_seconds=1,outputs=outputs))
        original = dict(request_id=request,operation_id=editor_id,stage="editor",source_attempt_count=1,
                        source_records_location="findings.json#/source_records",
                        research=dict(**research,result_sha256=result,started_at="original"))
        raw = encoded(original)
        def obj(key):
            data=(self.root/"stage_artifacts"/(key+".json")).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),key)
            return json.loads(data)
        view = referenced_editor_input(original,request,editor_id,obj,_require)
        self.assertEqual(view["sources"],[row])
        self.assertEqual(encoded(original),raw)
        self.assertNotIn("started_at",view["research"])
        original["research"]["outputs"]=[dict(name="findings.json",sha256="e"*64)]
        with self.assertRaisesRegex(BriefError,"digest differs"):
            referenced_editor_input(original,request,editor_id,obj,_require)
    def js(self, value):
        return self.artifact(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    def command(self, action, args, revision):
        command = dict(schema_version="research_control_v2",command_id=str(uuid4()),request_id=self.request,
                       action=action,owner_id=str(uuid4()),epoch=1,expected_revision=revision-1,args=args)
        digest = self.js(command)
        result = dict(command_id=command["command_id"],command_sha256=digest,request_id=self.request,status="committed",
                      run=dict(request_id=self.request,revision=revision,permit=None,plan_hash="a"*64),
                      dispatch_granted=action=="reserve",operations=[])
        if action == "reserve":
            result["operations"] = [{k: op[k] for k in ("operation_id","kind","key","input_sha256")} for op in args["operations"]]
        receipt = dict(type="control_observed",result=result)
        pair = dict(command_sha256=digest,receipt_sha256=self.js(receipt))
        self.pairs.append(pair)
        return command, receipt, pair

    def reserve(self, oid, kind, key, input_sha, input_value, seconds, revision):
        op = dict(operation_id=oid,kind=kind,key=key,input_sha256=input_sha,input=input_value,max_seconds=seconds)
        return self.command("reserve",dict(operations=[op]),revision)

    def settle(self, oid, file_sha, elapsed, revision, archive="b"*64):
        return self.command("settle",dict(operation_id=oid,outcome="acknowledged",elapsed_seconds=elapsed,
                                           archive_sha256=archive,file_sha256=file_sha,native_result=None),revision)

    def continuation(self, stage="research", previous_status="ok", prior_seconds=None, current_seconds=10, old_body_known=False):
        self.request = "morning-" + self.audit["date"].replace("-", "") + "-r1"
        self.pairs = []
        call = next(c for c in self.audit["model_calls"] if c["stage"] == stage)
        self.call = call
        prior = call["attempt_records"][0]
        if prior_seconds is not None:
            prior.update(elapsed_seconds=prior_seconds,reserved_seconds=1200)
        current_reservation=max(100,current_seconds)
        candidate_sha = self.artifact(encoded(self.candidates))
        draft_sha = self.artifact(encoded(self.draft))
        if stage == "research":
            editor = next(c for c in self.audit["model_calls"] if c["stage"] == "editor")
            editor["attempt_records"][-1]["result_sha256"] = self.js(dict(outputs=[dict(sha256=draft_sha)]))
        if stage == "editor":
            research = next(c for c in self.audit["model_calls"] if c["stage"] == "research")
            research["attempt_records"][-1]["result_sha256"] = self.js(dict(outputs=[dict(sha256=candidate_sha)]))
        self.prior_input=dict(request_id=self.request,stage=stage,sources=[])
        if stage == "editor":
            self.prior_input["research"] = dict(result_sha256=research["attempt_records"][-1]["result_sha256"],
                                               outputs=[dict(sha256=candidate_sha)])
        if old_body_known:
            self.prior_input["sources"].append(dict(body_sha256=self.audit["sources"][0]["content_sha256"]))
        prior["input_sha256"]=self.js(self.prior_input)
        prior_id, current_id, source_id = str(uuid4()), str(uuid4()), str(uuid4())
        error = "synthetic partial coverage" if previous_status == "partial" else ""
        prior.update(status=previous_status,error=error)
        prior_result = dict(request_id=self.request,operation_id=prior_id,stage=stage,input_sha256=prior["input_sha256"],
                            status=prior["status"],elapsed_seconds=prior["elapsed_seconds"],error=prior["error"])
        prior["result_sha256"] = self.js(prior_result)
        self.prior_reserve=self.reserve(prior_id,"model",stage,prior["input_sha256"],None,prior["reserved_seconds"],2)
        self.prior_settle=self.settle(prior_id,prior["result_sha256"],prior["elapsed_seconds"],3)
        source = self.audit["sources"][0]
        source_attempt = source["attempt_records"][0]
        source_input = source_identity(source["url"])["value"]
        input_sha = hashlib.sha256(json.dumps(source_input,separators=(",", ":"),ensure_ascii=False).encode()).hexdigest()
        source_result = dict(request_id=self.request,operation_id=source_id,input_sha256=input_sha,url=source["url"],status="ok",
                             elapsed_seconds=source_attempt["elapsed_seconds"],content_sha256=source["content_sha256"],
                             response_complete=True,curl_exit_code=0,http_status=200,error="")
        self.source_result = source_result
        source_sha = self.js(source_result)
        self.source_reserve = self.reserve(source_id,"source",input_sha,input_sha,source_input,source_attempt["reserved_seconds"],4)
        self.source_settle = self.settle(source_id,source_sha,source_attempt["elapsed_seconds"],5)
        self.manifest = dict(schema_version="model_prepublication_continuation_v1",request_id=self.request,stage=stage,
            previous_operation_id=prior_id,checkpoint_archive_sha256="b"*64,checkpoint_file_sha256=prior["result_sha256"],
            payload_sha256="0"*64,
            added_evidence=[dict(operation_id=source_id,archive_sha256="b"*64,file_sha256=source_sha)])
        self.payload=dict(request_id=self.request,stage=stage,sources=[dict(operation_id=source_id,
                          result_sha256=source_sha,body_sha256=source["content_sha256"])],
                          continuation={k:self.manifest[k] for k in ("previous_operation_id","checkpoint_archive_sha256","checkpoint_file_sha256","added_evidence")})
        self.manifest["payload_sha256"]=self.js(self.payload)
        manifest_sha = self.js(self.manifest)
        next_attempt = dict(status="ok",elapsed_seconds=current_seconds,error="",input_sha256=manifest_sha,
                            checkpoint_sha256=prior["result_sha256"],reserved_seconds=current_reservation)
        current_result = dict(request_id=self.request,operation_id=current_id,stage=stage,input_sha256=manifest_sha,
                              status="ok",elapsed_seconds=current_seconds,error="",
                              outputs=[dict(sha256=candidate_sha if stage == "research" else draft_sha)])
        next_attempt["result_sha256"] = self.js(current_result)
        input_value = {k:self.manifest[k] for k in ("previous_operation_id","checkpoint_archive_sha256","checkpoint_file_sha256")}
        input_value["continuation_archive_sha256"] = "c"*64
        self.current_reserve = self.reserve(current_id,"model",stage,manifest_sha,input_value,current_reservation,6)
        self.current_settle = self.settle(current_id, next_attempt["result_sha256"], current_seconds, 7)
        self.proof = dict(schema_version="model_prepublication_continuation_evidence_v1",operation_id=current_id,control_commands=self.pairs,
                          source_attempts=[dict(operation_id=source_id,source_id=source["id"],attempt_index=0)])
        next_attempt["prepublication_continuation_sha256"] = self.js(self.proof)
        call.update(attempts=2,elapsed_seconds=prior["elapsed_seconds"]+current_seconds,status="ok",error="",attempt_records=[prior,next_attempt])
        return call

    def refresh_proof(self):
        self.call["attempt_records"][-1]["prepublication_continuation_sha256"] = self.js(self.proof)

    def resumed_control(self, name, expired=True, intermediate=False):
        self.continuation()
        for label in ("prior_reserve", "prior_settle", "source_reserve", "source_settle", "current_reserve", "current_settle"):
            command, receipt, _ = triple = getattr(self, label)
            receipt["result"]["run"]["revision"] *= 10
            command["expected_revision"] = receipt["result"]["run"]["revision"] - 1
            self.refresh_control(triple)
        command, receipt, _ = original = getattr(self, name + "_reserve")
        operation = command["args"]["operations"][0]
        if expired:
            receipt["result"].update(dispatch_granted=False, grant_expired=True)
            self.refresh_control(original)
        revision = receipt["result"]["run"]["revision"]
        for offset in range(1, 3 if intermediate else 2):
            resumed = self.command("resume", dict(operation_id=operation["operation_id"]), revision + offset)
            result = resumed[1]["result"]
            result["operations"] = [{k: operation[k] for k in ("operation_id", "kind", "key", "input_sha256")}]
            result["dispatch_granted"] = not intermediate or offset == 2
            if not result["dispatch_granted"]:
                result["grant_expired"] = True
            self.refresh_control(resumed)
        self.refresh_proof()
        return resumed

    def test_resumed_original_operations_preserve_attempts_and_charges(self):
        for name in ("prior", "source", "current"):
            for expired, intermediate in ((False, False), (True, False), (True, True)):
                with self.subTest(operation=name, expired=expired, intermediate=intermediate):
                    self.setUp()
                    expected = self.check()["budget_accounting"]["charged_seconds"] + 10
                    self.resumed_control(name, expired, intermediate)
                    result = self.check()
                    self.assertEqual(result["readiness"], "ready")
                    self.assertEqual(result["budget_accounting"]["charged_seconds"], expected)
                    self.assertEqual(self.call["attempts"], 2)

    def test_resume_requires_original_operation_binding(self):
        for field, value in (("operation_id", str(uuid4())), ("kind", "native"),
                             ("key", "foreign"), ("input_sha256", "f"*64)):
            with self.subTest(field=field):
                self.setUp()
                resumed = self.resumed_control("source")
                resumed[1]["result"]["operations"][0][field] = value
                self.refresh_control(resumed); self.refresh_proof()
                self.reject("resume")
        self.setUp()
        self.resumed_control("source")
        self.pairs.remove(self.source_reserve[2]); self.refresh_proof()
        self.reject("original reservation")

    def test_resume_requires_fresh_receipt_before_settlement(self):
        for change in ({"dispatch_granted": False}, {"replayed": True},
                       {"dispatch_granted": False, "grant_expired": True}, {"grant_expired": True}):
            for expired in (False, True):
                with self.subTest(change=change, expired=expired):
                    self.setUp()
                    resumed = self.resumed_control("current", expired=expired)
                    resumed[1]["result"].update(change)
                    self.refresh_control(resumed); self.refresh_proof()
                    self.reject("fresh reserve|control command/receipt")
        for revision in (39, 51):
            with self.subTest(revision=revision):
                self.setUp()
                resumed = self.resumed_control("source")
                resumed[0]["expected_revision"] = revision - 1
                resumed[1]["result"]["run"]["revision"] = revision
                self.refresh_control(resumed); self.refresh_proof()
                self.reject("before settlement")

    def test_research_and_editor_can_extend_original_success_with_new_evidence(self):
        for stage, status in (("research","ok"),("research","partial"),("editor","ok")):
            with self.subTest(stage=stage,status=status):
                self.setUp()
                baseline=self.check()["budget_accounting"]["charged_seconds"]
                self.continuation(stage,status)
                result=self.check()
                self.assertEqual(result["readiness"],"ready")
                self.assertEqual(result["budget_accounting"]["charged_seconds"],baseline+10)

    def test_success_without_continuation_proof_still_rejected(self):
        call=self.continuation()
        del call["attempt_records"][-1]["prepublication_continuation_sha256"]
        self.reject("Cannot retry")

    def test_control_receipt_mutations_rejected(self):
        for mutation in (dict(dispatch_granted=False),dict(replayed=True),dict(status="rejected"),dict(command_sha256="f"*64)):
            with self.subTest(mutation=mutation):
                self.setUp(); self.continuation()
                _, receipt, pair=self.current_reserve
                receipt["result"].update(mutation)
                pair["receipt_sha256"]=self.js(receipt); self.refresh_proof()
                self.reject("Model continuation")

    def test_source_cannot_be_older_than_previous_model(self):
        self.continuation()
        command,receipt,pair=self.source_reserve
        command["expected_revision"]=0;receipt["result"]["run"]["revision"]=1
        pair["command_sha256"]=self.js(command);receipt["result"]["command_sha256"]=pair["command_sha256"]
        pair["receipt_sha256"]=self.js(receipt);self.refresh_proof()
        self.reject("evidence must be reserved after")

    def test_evidence_settlement_must_precede_current_grant(self):
        self.continuation()
        command,receipt,pair=self.source_settle
        command["expected_revision"]=7;receipt["result"]["run"]["revision"]=8
        pair["command_sha256"]=self.js(command);receipt["result"]["command_sha256"]=pair["command_sha256"]
        pair["receipt_sha256"]=self.js(receipt);self.refresh_proof()
        self.reject("settlement order")

    def test_source_attempt_identity_and_content_are_required(self):
        for kind in ("operation", "content"):
            with self.subTest(kind=kind):
                self.setUp();self.continuation()
                source=self.audit["sources"][0]
                if kind=="operation":
                    self.proof["source_attempts"][0]["source_id"]="unknown";self.refresh_proof()
                else: self.root.joinpath("evidence",source["id"]+".txt").write_text("tampered",encoding="utf-8")
                self.reject("source" if kind=="operation" else "fingerprint")

    def test_continuation_preserves_attempt_limit(self):
        self.continuation();self.audit["limits"]["model_attempts"]=1
        self.assertIn("model_budget_exceeded",self.check()["blockers"])

    def test_continuation_proof_cannot_appear_on_initial_model_attempt(self):
        call=self.continuation()
        call["attempt_records"][0]["prepublication_continuation_sha256"]=call["attempt_records"][1]["prepublication_continuation_sha256"]
        self.reject("continuation")

    def test_successful_stage_cumulative_cap_and_boundary(self):
        for current_seconds,blocked in ((800,False),(1000,True)):
            with self.subTest(current_seconds=current_seconds):
                self.setUp()
                self.audit["finished_at"]="2026-11-06T08:20:00+08:00"
                self.audit["limits"].update(model_stage_seconds=1800,run_seconds=3600)
                self.continuation(prior_seconds=1000,current_seconds=current_seconds)
                result=self.check()
                self.assertEqual("model_budget_exceeded" in result["blockers"],blocked)

    def test_invalid_source_mapping_indices_and_duplicates_rejected(self):
        for index in (-1,True,10000):
            with self.subTest(index=index):
                self.setUp();self.continuation()
                self.proof["source_attempts"][0]["attempt_index"]=index
                self.refresh_proof();self.reject("Model continuation")
        self.setUp();self.continuation()
        self.proof["source_attempts"].append(dict(self.proof["source_attempts"][0]))
        self.refresh_proof();self.reject("unique")

    def test_original_checkpoint_cannot_be_replaced(self):
        call=self.continuation()
        call["attempt_records"][-1]["checkpoint_sha256"]=self.result
        self.reject("checkpoint")

    def test_extra_unknown_control_reference_rejected(self):
        self.continuation()
        self.proof["control_commands"].append(dict(command_sha256="f"*64,receipt_sha256="e"*64))
        self.refresh_proof();self.reject("Missing")

    def refresh_control(self, triple):
        command,receipt,pair=triple
        pair["command_sha256"]=self.js(command)
        receipt["result"]["command_sha256"]=pair["command_sha256"]
        if command["action"]=="reserve":
            receipt["result"]["operations"]=[{k:op[k] for k in ("operation_id","kind","key","input_sha256")} for op in command["args"]["operations"]]
        pair["receipt_sha256"]=self.js(receipt)

    def refresh_manifest(self, refresh_payload=True):
        if refresh_payload:
            self.payload["continuation"]={k:self.manifest[k] for k in ("previous_operation_id","checkpoint_archive_sha256","checkpoint_file_sha256","added_evidence")}
            self.manifest["payload_sha256"]=self.js(self.payload)
        attempt=self.call["attempt_records"][-1]
        digest=self.js(self.manifest)
        attempt["input_sha256"]=digest
        result=json.loads(self.root.joinpath("stage_artifacts",attempt["result_sha256"]+".json").read_bytes())
        result["input_sha256"]=digest
        attempt["result_sha256"]=self.js(result)
        self.current_reserve[0]["args"]["operations"][0]["input_sha256"]=digest
        self.refresh_control(self.current_reserve)
        self.current_settle[0]["args"]["file_sha256"] = attempt["result_sha256"]
        self.refresh_control(self.current_settle)
        self.refresh_proof()

    def editor_research_continuation(self):
        self.continuation("editor")
        research=next(c for c in self.audit["model_calls"] if c["stage"]=="research")
        original=research["attempt_records"][0]
        evidence=self.manifest["added_evidence"][0]
        result=dict(request_id=self.request,operation_id=evidence["operation_id"],stage="research",
                    input_sha256=original["input_sha256"],status="ok",elapsed_seconds=original["elapsed_seconds"],error="",
                    outputs=[dict(sha256=self.artifact(encoded({"candidates": self.candidates})))])
        evidence["file_sha256"]=original["result_sha256"]=self.js(result)
        op=self.source_reserve[0]["args"]["operations"][0]
        op.update(kind="model",key="research",input_sha256=original["input_sha256"],input={},max_seconds=original["reserved_seconds"])
        self.refresh_control(self.source_reserve)
        self.source_settle[0]["args"].update(file_sha256=evidence["file_sha256"],elapsed_seconds=original["elapsed_seconds"])
        self.refresh_control(self.source_settle)
        self.proof["source_attempts"]=[]
        self.payload["sources"]=[]
        self.payload["research"]=dict(operation_id=evidence["operation_id"],result_sha256=evidence["file_sha256"],outputs=[dict(output) for output in result["outputs"]])
        self.refresh_manifest()
        return result

    def check_evidence(self):
        def read(path, limit):
            target = self.root/path
            if not target.is_file():
                raise BriefError("Missing audit file")
            return target.read_bytes()
        return validate_continuation(self.audit, self.call, self.call["attempt_records"][-1],
                                     self.call["attempt_records"][-2], read, _require, _json)

    def test_editor_evidence_can_consume_new_research_output(self):
        result=self.editor_research_continuation()
        self.assertTrue(self.check_evidence())
        self.root.joinpath("stage_artifacts",result["outputs"][0]["sha256"]+".json").unlink()
        self.reject("research output bytes|Missing audit file")

    def test_sources_need_complete_successful_transport(self):
        for update in (dict(status="failed"),dict(response_complete=False),dict(curl_exit_code=1),dict(http_status=302),dict(error="blocked")):
            with self.subTest(update=update):
                self.setUp();self.continuation()
                self.source_result.update(update)
                digest=self.js(self.source_result)
                self.manifest["added_evidence"][0]["file_sha256"]=digest
                self.source_settle[0]["args"]["file_sha256"]=digest
                self.refresh_control(self.source_settle);self.refresh_manifest()
                self.reject("complete successful response")

    def test_empty_payload_and_wrong_stage_rejected(self):
        self.continuation()
        self.manifest["payload_sha256"]=self.artifact(b"")
        self.refresh_manifest(False);self.reject("Invalid model continuation JSON")
        self.setUp();self.continuation()
        self.manifest["stage"]="editor"
        self.refresh_manifest();self.reject("manifest schema or stage")

    def test_ordinary_unknown_model_timing_preserves_original_policy(self):
        call=self.audit["model_calls"][0]
        call.update(elapsed_seconds=None,timing_unavailable_reason="synthetic timing loss")
        call["attempt_records"][0].update(elapsed_seconds=None,timing_unavailable_reason="synthetic timing loss",reserved_seconds=70)
        result=self.check()
        self.assertEqual(result["readiness"],"ready")
        self.assertNotIn("operation_outcome_unknown",result["blockers"])
        self.assertEqual(result["budget_accounting"]["reserved_unknown_seconds"],70)
        self.assertEqual(result["budget_accounting"]["unknown_attempts"]["model"],1)

    def test_stage_cap_stays_separate_for_authenticated_page_rounds(self):
        case=successor_tests.PageSuccessorTests()
        try:
            case.setUp()
            case.audit["finished_at"]="2026-11-06T08:20:00+08:00"
            call=case.audit["model_calls"][0]
            attempt=call["attempt_records"][-1]
            cap=case.audit["limits"]["model_stage_seconds"]
            call["elapsed_seconds"]+=cap-attempt["elapsed_seconds"]
            attempt.update(elapsed_seconds=cap,reserved_seconds=cap)
            self.assertNotIn("model_budget_exceeded",case.check()["blockers"])
        finally:
            case.doCleanups()

    def test_later_page_retains_original_prepublication_proof(self):
        successor_tests.scoped.PageScopeTests.setUp(self)
        self.continuation()
        self.assertEqual(self.check()["readiness"],"partial")
        successor_tests.PageSuccessorTests.promote(self,"hardware-status")
        for call in self.audit["model_calls"]:
            call["attempt_records"][-1].pop("prepublication_continuation_sha256",None)
        self.assertEqual(self.check()["readiness"],"partial")
        retained=self.audit["model_calls"][0]["attempt_records"][-2]["prepublication_continuation_sha256"]
        self.audit["model_calls"][0]["attempt_records"][-1]["prepublication_continuation_sha256"]=retained
        self.reject("before first publication")

    def test_repeated_content_under_a_new_operation_is_not_incremental(self):
        self.continuation(old_body_known=True)
        self.reject("new response bytes")

    def test_whitespace_changed_old_payload_does_not_authorize_continuation(self):
        self.continuation()
        original=self.root.joinpath("stage_artifacts",self.call["attempt_records"][0]["input_sha256"]+".json").read_bytes()
        self.manifest["payload_sha256"]=self.artifact(original+b" ")
        self.refresh_manifest(False)
        self.reject("payload checkpoint and evidence binding")

    def test_payload_must_reference_each_added_response(self):
        for field in ("operation_id","result_sha256","body_sha256"):
            with self.subTest(field=field):
                self.setUp();self.continuation()
                self.payload["sources"][0][field]=str(uuid4()) if field=="operation_id" else "f"*64
                self.refresh_manifest();self.reject("payload source")
        self.setUp();self.continuation()
        self.payload["sources"].append(dict(self.payload["sources"][0]))
        self.refresh_manifest();self.reject("payload source")

    def test_payload_requires_checkpoint_and_exact_added_evidence_binding(self):
        self.continuation()
        self.payload["continuation"]["checkpoint_file_sha256"]="f"*64
        self.manifest["payload_sha256"]=self.js(self.payload)
        self.refresh_manifest(False)
        self.reject("payload checkpoint and evidence binding")

    def refresh_prior_input(self):
        prior,current=self.call["attempt_records"]
        prior["input_sha256"]=self.js(self.prior_input)
        result=json.loads(self.root.joinpath("stage_artifacts",prior["result_sha256"]+".json").read_bytes())
        result["input_sha256"]=prior["input_sha256"]
        prior["result_sha256"]=current["checkpoint_sha256"]=self.js(result)
        self.manifest["checkpoint_file_sha256"]=prior["result_sha256"]
        self.current_reserve[0]["args"]["operations"][0]["input"]["checkpoint_file_sha256"]=prior["result_sha256"]
        self.prior_reserve[0]["args"]["operations"][0]["input_sha256"]=prior["input_sha256"]
        self.prior_settle[0]["args"]["file_sha256"]=prior["result_sha256"]
        self.refresh_control(self.prior_reserve);self.refresh_control(self.prior_settle);self.refresh_manifest()

    def two_stage_source_proofs(self, separate_attempts=False):
        research = self.continuation()
        retained = deepcopy(research)
        self.continuation("editor")
        research.clear()
        research.update(retained)
        current = retained["attempt_records"][-1]
        result = json.loads((self.root / "stage_artifacts" / (current["result_sha256"] + ".json")).read_bytes())
        context = dict(result_sha256=current["result_sha256"], outputs=result["outputs"])
        self.prior_input["research"] = deepcopy(context)
        self.payload["research"] = deepcopy(context)
        for control in (self.prior_reserve, self.prior_settle, self.source_reserve,
                        self.source_settle, self.current_reserve, self.current_settle):
            control[0]["expected_revision"] += 8
            control[1]["result"]["run"]["revision"] += 8
            self.refresh_control(control)
        if separate_attempts:
            source = self.audit["sources"][0]
            source["attempt_records"].append(deepcopy(source["attempt_records"][0]))
            source["attempts"] += 1
            source["elapsed_seconds"] += source["attempt_records"][-1]["elapsed_seconds"]
            self.proof["source_attempts"][0]["attempt_index"] = 1
        self.refresh_prior_input()

    def test_distinct_reads_cannot_share_one_attempt_between_stages(self):
        self.two_stage_source_proofs()
        self.reject("unique across continuation proofs")

    def test_distinct_reads_with_distinct_attempts_preserve_accounting(self):
        self.two_stage_source_proofs(separate_attempts=True)
        result = self.check()
        self.assertEqual(result["readiness"], "ready")
        self.assertEqual(result["budget_accounting"]["successful_attempts"]["source"],
                         sum(len(source["attempt_records"]) for source in self.audit["sources"]))

    def test_same_operation_reference_can_be_reused_in_continuation_proofs(self):
        call = self.continuation()
        bindings = ({}, {})
        for _ in range(2):
            self.assertTrue(validate_continuation(
                self.audit, call, call["attempt_records"][-1], call["attempt_records"][0],
                lambda path, limit: (self.root / path).read_bytes(), _require, _json, bindings))

    def test_identical_research_output_does_not_become_new_with_result_wrapper(self):
        result=self.editor_research_continuation()
        self.prior_input["research"]=dict(outputs=result["outputs"])
        self.refresh_prior_input()
        self.reject("new output bytes")

    def test_research_output_must_be_referenced_in_research_not_unrelated_sources(self):
        result=self.editor_research_continuation()
        self.payload["sources"]=[dict(body_sha256=result["outputs"][0]["sha256"])]
        self.payload["research"]["outputs"]=[]
        self.refresh_manifest()
        self.reject("actual research outputs")

    def test_continuation_unknown_timing_blocks_and_keeps_reservation(self):
        call=self.continuation()
        call.update(elapsed_seconds=None,timing_unavailable_reason="synthetic timing loss")
        attempt=call["attempt_records"][-1]
        attempt.update(elapsed_seconds=None,timing_unavailable_reason="synthetic timing loss")
        result=json.loads(self.root.joinpath("stage_artifacts",attempt["result_sha256"]+".json").read_bytes())
        result["elapsed_seconds"]=None
        attempt["result_sha256"]=self.js(result)
        self.proof["control_commands"].remove(self.current_settle[2]); self.refresh_proof()
        audited=self.check()
        self.assertEqual(audited["readiness"],"blocked")
        self.assertIn("operation_outcome_unknown",audited["blockers"])
        self.assertEqual(audited["budget_accounting"]["reserved_unknown_seconds"],attempt["reserved_seconds"])

    def update_research_result(self, result):
        evidence=self.manifest["added_evidence"][0]
        digest=self.js(result)
        research=next(call for call in self.audit["model_calls"] if call["stage"]=="research")
        research["attempt_records"][0]["result_sha256"]=digest
        evidence["file_sha256"]=digest
        self.payload["research"]["result_sha256"]=digest
        self.source_settle[0]["args"]["file_sha256"]=digest
        self.refresh_control(self.source_settle);self.refresh_manifest()

    def test_research_scalar_output_and_union_are_supported(self):
        for scalar_only in (True,False):
            with self.subTest(scalar_only=scalar_only):
                self.setUp();result=self.editor_research_continuation()
                scalar=self.artifact(encoded({"candidates": self.candidates, "notes": "scalar fixture"}))
                result["output_sha256"]=scalar
                if scalar_only:
                    del result["outputs"]
                    self.payload["research"]["outputs"]=[]
                self.payload["research"]["outputs"].append(dict(sha256=scalar))
                self.update_research_result(result)
                self.assertTrue(self.check_evidence())

    def test_research_payload_cannot_add_undeclared_output(self):
        self.editor_research_continuation()
        self.payload["research"]["outputs"].append(dict(sha256=self.artifact(b'{"unverified":true}')))
        self.refresh_manifest();self.reject("exactly the actual research outputs")

    def test_every_declared_output_must_be_valid_and_have_bytes(self):
        for change in (dict(output_sha256="invalid"),dict(output_sha256="f"*64),dict(outputs=None),dict(outputs=[dict(sha256="invalid")])):
            with self.subTest(change=change):
                self.setUp();result=self.editor_research_continuation();result.update(change)
                self.update_research_result(result)
                self.reject("declared research|Missing audit file")

    def test_stage_input_rejects_inline_or_unhashed_source_context(self):
        invalid = [{"body": "old body"}, {"body_sha256": "a"*64, "text": "old"},
                   {"body_sha256": None, "status": "ok"}, {"body_sha256": None, "body_file": "old.txt"}]
        for source in invalid:
            with self.subTest(source=source):
                self.setUp(); self.continuation()
                self.prior_input["sources"] = [source]; self.refresh_prior_input()
                self.reject("stage input structure")

    def test_stage_input_rejects_unsupported_research_context(self):
        for research in (None, "inline", {"text": "inline"}, {"outputs": [{"sha256": "a"*64, "text": "inline"}]}):
            with self.subTest(research=research):
                self.setUp(); self.continuation()
                self.prior_input["research"] = research; self.refresh_prior_input()
                self.reject("research input")

    def test_payload_source_cannot_claim_another_url_or_input(self):
        for field, value in (("url", "https://example.net/not-original"), ("input_sha256", "a"*64)):
            with self.subTest(field=field):
                self.setUp(); self.continuation()
                self.payload["sources"][0][field] = value; self.refresh_manifest()
                self.reject("source identity")

    def test_failed_empty_source_context_is_supported(self):
        self.continuation()
        self.prior_input["sources"] = [{"body_sha256": None, "status": "failed", "body_file": None}]
        self.refresh_prior_input()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_payload_source_allows_canonical_equivalent_url(self):
        self.continuation()
        original = self.audit["sources"][0]["url"]
        self.payload["sources"][0]["url"] = original.replace("https://", "HTTPS://")
        self.refresh_manifest()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_payload_source_rejects_fragments_and_credentials(self):
        for url in ("https://example.org/change#fragment", "https://user:pass@example.org/change"):
            with self.subTest(url=url):
                self.setUp(); self.continuation()
                self.payload["sources"][0]["url"] = url; self.refresh_manifest()
                self.reject("source identity")

    def test_current_continuation_cannot_claim_another_candidate_output(self):
        self.continuation()
        self.candidates["items"][0]["summary"] = "Changed after model output"
        self.reject("bind final candidates")

    def test_current_editor_cannot_claim_another_draft_output(self):
        self.continuation("editor")
        self.draft["entries"][0]["title"] = "Changed after editor output"
        self.reject("bind final candidates or draft")

    def test_editor_only_cannot_introduce_candidates_but_allows_notes(self):
        self.continuation("editor")
        self.candidates["notes"] = ["Metadata-only formatting"]
        self.assertEqual(self.check()["readiness"], "ready")
        self.candidates["items"][0]["summary"] = "New research claim"
        self.reject("cannot introduce new research candidates")

    def test_new_research_output_can_bind_changed_candidates(self):
        self.candidates["items"][0]["summary"] = "New model interpretation of retained evidence"
        self.continuation()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_editor_only_cannot_replace_retained_research_to_authorize_candidates(self):
        self.continuation("editor")
        self.candidates["items"][0]["summary"] = "Changed candidate"
        research = next(c for c in self.audit["model_calls"] if c["stage"] == "research")
        changed_sha = self.artifact(encoded(self.candidates))
        research["attempt_records"][-1]["result_sha256"] = self.js(dict(outputs=[dict(sha256=changed_sha)]))
        self.reject("prior checkpoint must match")

    def test_payload_cannot_smuggle_unverified_source_context(self):
        self.continuation()
        self.payload["sources"].append(dict(operation_id=str(uuid4()), body_sha256="f"*64))
        self.refresh_manifest()
        self.reject("unverified new source")

    def test_payload_cannot_smuggle_unverified_research_context(self):
        self.continuation()
        self.payload["research"] = dict(outputs=[dict(sha256="f"*64)])
        self.refresh_manifest()
        self.reject("unverified new research")

    def test_payload_can_retain_original_research_context(self):
        self.continuation("editor")
        self.payload["research"] = self.prior_input["research"]
        self.refresh_manifest()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_research_outputs_null_is_rejected_as_shape(self):
        self.continuation()
        self.prior_input["research"] = dict(outputs=None)
        self.refresh_prior_input()
        self.reject("research input outputs")

    def test_current_success_requires_matching_original_settlement(self):
        self.continuation()
        self.current_settle[0]["args"]["file_sha256"] = "f"*64
        self.refresh_control(self.current_settle); self.refresh_proof()
        self.reject("settlement order")

    def test_unrelated_body_cannot_replace_last_executed_source(self):
        self.continuation()
        old = self.source_result["content_sha256"]
        new = self.artifact(b"Different authenticated source bytes")
        self.root.joinpath("evidence", new+".txt").write_bytes(b"Different authenticated source bytes")
        self.source_result["content_sha256"] = new
        sha = self.js(self.source_result)
        self.source_settle[0]["args"]["file_sha256"] = sha
        self.refresh_control(self.source_settle)
        self.manifest["added_evidence"][0]["file_sha256"] = sha
        self.payload["sources"][0].update(body_sha256=new,result_sha256=sha)
        self.refresh_manifest()
        self.reject("last executed source result")

    def test_native_zero_reservation_sibling_preserves_original_command(self):
        self.continuation()
        command, receipt, pair = self.source_reserve
        op = dict(operation_id=str(uuid4()),kind="native",key="fixture",input_sha256="f"*64,input={},max_seconds=0)
        command["args"]["operations"].append(op)
        receipt["result"]["operations"].append({k:op[k] for k in ("operation_id","kind","key","input_sha256")})
        self.refresh_control(self.source_reserve); self.refresh_proof()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_same_reserve_batch_uses_authenticated_operation_order(self):
        self.continuation()
        prior_command, prior_receipt, _ = self.prior_reserve
        source_op = self.source_reserve[0]["args"]["operations"][0]
        source_grant = self.source_reserve[1]["result"]["operations"][0]
        prior_command["args"]["operations"].append(source_op)
        prior_receipt["result"]["operations"].append(source_grant)
        self.proof["control_commands"].remove(self.source_reserve[2])
        self.refresh_control(self.prior_reserve); self.refresh_proof()
        self.assertEqual(self.check()["readiness"], "ready")
        prior_command["args"]["operations"].reverse()
        self.refresh_control(self.prior_reserve); self.refresh_proof()
        self.reject("reserved after")

    def test_duplicate_keys_and_nonfinite_proof_are_rejected(self):
        for suffix in (',"schema_version":"model_prepublication_continuation_evidence_v1"', ',"invalid":NaN'):
            with self.subTest(suffix=suffix):
                self.setUp(); self.continuation()
                raw=json.dumps(self.proof)[:-1]+suffix+'}'
                self.call["attempt_records"][-1]["prepublication_continuation_sha256"]=self.artifact(raw.encode())
                self.reject("Invalid model continuation JSON")

    def test_malformed_extra_source_id_is_rejected_without_traceback(self):
        self.continuation()
        self.payload["sources"].append(dict(operation_id=[],body_sha256="f"*64))
        self.refresh_manifest()
        self.reject("unverified new source")

    def test_research_only_cannot_rewrite_editor_draft(self):
        self.continuation()
        self.draft["entries"][0]["title"] = "Unexecuted editor rewrite"
        self.reject("bind final candidates or draft")

    def test_wrapped_research_candidates_are_supported(self):
        self.continuation()
        attempt=self.call["attempt_records"][-1]
        result=json.loads(self.root.joinpath("stage_artifacts",attempt["result_sha256"]+".json").read_bytes())
        result.pop("outputs")
        result["output_sha256"]=self.artifact(encoded(dict(candidates=self.candidates,checks=self.audit["checks"],sources=self.audit["sources"])))
        attempt["result_sha256"]=self.js(result)
        self.current_settle[0]["args"]["file_sha256"]=attempt["result_sha256"]
        self.refresh_control(self.current_settle);self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")

    def test_source_grant_accepts_canonical_query_order(self):
        old=self.audit["sources"][0]["url"]
        new=old+"?z=2&a=1"
        def replace(value):
            if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
            if isinstance(value,list): return [replace(v) for v in value]
            return new if value==old else value
        self.audit=replace(self.audit);self.candidates=replace(self.candidates);self.draft=replace(self.draft)
        self.continuation()
        self.source_reserve[0]["args"]["operations"][0]["input"]["url"] = new
        self.refresh_control(self.source_reserve); self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")

    def test_failed_continuation_retry_retains_output_requirements(self):
        self.continuation()
        failed=self.call["attempt_records"][-1]
        failed.update(status="failed",error="synthetic model failure")
        result=json.loads(self.root.joinpath("stage_artifacts",failed["result_sha256"]+".json").read_bytes())
        result.update(status="failed",error=failed["error"])
        failed["result_sha256"]=self.js(result)
        self.current_settle[0]["args"]["file_sha256"]=failed["result_sha256"]
        self.refresh_control(self.current_settle);self.refresh_proof()
        retry=dict(status="ok",elapsed_seconds=5,error="",reserved_seconds=100,
                   input_sha256=self.js(dict(synthetic="retry input")),checkpoint_sha256=failed["result_sha256"],
                   result_sha256=self.js(dict(status="ok",outputs=[])))
        self.call["attempt_records"].append(retry)
        self.call.update(attempts=3,elapsed_seconds=self.call["elapsed_seconds"]+5,status="ok",error="")
        self.reject("outputs must bind")
        retry["result_sha256"]=self.js(dict(status="ok",outputs=[dict(sha256=self.artifact(encoded(self.candidates)))]))
        self.assertEqual(self.check()["readiness"],"ready")

    def test_output_binding_requires_editor_to_consume_changed_research(self):
        self.continuation("editor")
        research=next(call for call in self.audit["model_calls"] if call["stage"]=="research")
        old=deepcopy(research["attempt_records"][0])
        self.candidates["items"][0]["summary"]="New research candidates"
        current=deepcopy(old)
        current.update(prepublication_continuation_sha256="f"*64,
                       result_sha256=self.js(dict(outputs=[dict(sha256=self.artifact(encoded(self.candidates)))])))
        research["attempt_records"].append(current)
        self.save()
        read=lambda relative,limit:self.root.joinpath(relative).read_bytes()
        with self.assertRaisesRegex(BriefError,"must consume latest research"):
            validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        self.payload["research"]=dict(result_sha256=current["result_sha256"],outputs=[dict(sha256=self.artifact(encoded(self.candidates)))])
        self.refresh_manifest();self.save()
        validate_current_outputs(self.audit,self.candidates,read,_require,_json)

    def test_partial_continuation_keeps_wrapped_coverage_resolution(self):
        ResolvedCoverageTests.setUp(self)
        self.continuation()
        attempt=self.call["attempt_records"][-1]
        result=dict(self.original,request_id=self.request,operation_id=self.proof["operation_id"],
                    input_sha256=attempt["input_sha256"],elapsed_seconds=attempt["elapsed_seconds"],
                    status="partial",stage_status="partial",started_at="2026-11-06T07:22:50+08:00")
        attempt.update(status="partial",error=result["error"],result_sha256=self.js(result))
        self.call.update(status="partial",error=result["error"],partial_reason="coverage_incomplete")
        self.current_settle[0]["args"]["file_sha256"]=attempt["result_sha256"]
        self.refresh_control(self.current_settle);self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")

    def test_distinct_control_commands_cannot_share_revision(self):
        self.continuation()
        command, receipt, pair = self.source_settle
        command["expected_revision"] = 2
        receipt["result"]["run"]["revision"] = 3
        self.refresh_control(self.source_settle); self.refresh_proof()
        self.reject("cannot share a committed revision")

    def test_payload_top_level_context_is_retained_or_omitted(self):
        self.continuation()
        self.prior_input["as_of"] = "2026-11-06T07:00:00+08:00"
        self.refresh_prior_input()
        self.payload["as_of"] = self.prior_input["as_of"]
        self.refresh_manifest()
        self.assertEqual(self.check()["readiness"], "ready")
        self.payload["as_of"] = "2026-11-07T07:00:00+08:00"
        self.refresh_manifest(); self.reject("unverified top-level")
        del self.payload["as_of"]
        self.refresh_manifest()
        self.assertEqual(self.check()["readiness"], "ready")
        self.payload["inline_material"] = "Unverified claims"
        self.refresh_manifest(); self.reject("unverified top-level")

    def test_payload_optional_source_outcomes_match_result(self):
        for field, value in (("status", "timeout"), ("elapsed_seconds", 99999), ("elapsed_seconds", True)):
            with self.subTest(field=field, value=value):
                self.setUp(); self.continuation()
                self.payload["sources"][0][field] = value
                self.refresh_manifest(); self.reject("source outcome")
        self.setUp(); self.continuation()
        self.payload["sources"][0].update(status="ok", elapsed_seconds=float(self.source_result["elapsed_seconds"]))
        self.refresh_manifest()
        self.assertEqual(self.check()["readiness"], "ready")

    def test_standalone_current_candidates_require_exact_notes(self):
        self.continuation()
        self.candidates["notes"] = ["Not the model output"]
        self.reject("bind final candidates")

    def test_continuation_verifies_large_declared_outputs_sequentially(self):
        import tracemalloc
        hashes = []
        for index in range(12):
            data = bytes([65 + index]) * 4_000_000
            hashes.append(self.artifact(data))
        del data
        candidates = {"mode": "live", "items": []}
        hashes.append(self.js({"candidates": candidates}))
        result = self.js({"outputs": [{"sha256": digest} for digest in hashes]})
        attempt = {"status": "ok", "result_sha256": result, "prepublication_continuation_sha256": "a"*64}
        metadata = {"model_calls": [{"stage": "research", "attempt_records": [attempt]}],
                    "inputs": {"candidates_sha256": "b"*64}}
        def read(path, limit):
            return (self.root / path).read_bytes()
        tracemalloc.start()
        try:
            validate_current_outputs(metadata, candidates, read, _require, _json)
            self.assertLess(tracemalloc.get_traced_memory()[1], 24_000_000)
        finally:
            tracemalloc.stop()
        (self.root / "stage_artifacts" / (hashes[3] + ".json")).write_bytes(b"altered")
        with self.assertRaisesRegex(BriefError, "fingerprint mismatch"):
            validate_current_outputs(metadata, candidates, read, _require, _json)

    def validate_only_continuation(self, **options):
        read=lambda relative,limit:self.root.joinpath(relative).read_bytes()
        return validate_continuation(self.audit,self.call,self.call["attempt_records"][-1],
                                     self.call["attempt_records"][-2],read,_require,_json,**options)

    def test_migration_requires_the_original_continuation_plan(self):
        self.continuation(previous_status="partial")
        self.assertTrue(self.validate_only_continuation(expected_plan_hash="a"*64))
        with self.assertRaisesRegex(BriefError, "same plan"):
            self.validate_only_continuation(expected_plan_hash="b"*64)

    def test_research_continues_from_the_original_nested_settlement(self):
        self.continuation(previous_status="partial")
        oid = str(uuid4())
        body = {"bytes": 3, "sha256": hashlib.sha256(b"old").hexdigest()}
        row = dict(operation_id=oid, source_id="old", url="https://example.org/old",
                   input_sha256=source_identity("https://example.org/old")["hash"], status="ok", elapsed_seconds=2, artifacts={"response.body.bin": body})
        raw = encoded(dict(row, request_id=self.request, content_sha256=body["sha256"]))
        row["raw_result"] = dict(bytes=len(raw), sha256=self.artifact(raw))
        args = dict(operation_id=oid, archive_sha256="2"*64, file_sha256=row["raw_result"]["sha256"],
                    outcome="acknowledged", elapsed_seconds=2)
        command = encoded(dict(action="settle", command_id=str(uuid4()), request_id=self.request, args=args))
        row["settlement"] = dict(original_command=dict(bytes=len(command), sha256=self.artifact(command)),
                                 archive_sha256=args["archive_sha256"], result_sha256=args["file_sha256"])
        self.prior_input["sources"] = [row]
        self.refresh_prior_input()
        self.assertTrue(self.validate_only_continuation())
        for command, receipt, pair in (self.current_reserve, self.current_settle):
            command["expected_revision"] += 2
            receipt["result"]["run"]["revision"] += 2
            self.refresh_control((command, receipt, pair))
        failed_id = str(uuid4())
        failed_input = source_identity("https://example.org/failed")
        empty = hashlib.sha256(b"").hexdigest()
        failure = dict(request_id=self.request, operation_id=failed_id, input_sha256=failed_input["hash"],
                       url=failed_input["value"]["url"], status="failed", elapsed_seconds=2, error="TLS", content_sha256=empty,
                       dispatch_started=True, attempts_in_this_operation=1, curl_exit_code=35, http_status=0)
        failure_sha = self.js(failure)
        self.reserve(failed_id, "source", failed_input["hash"], failed_input["hash"], failed_input["value"], 10, 6)
        self.settle(failed_id, failure_sha, 2, 7)
        self.audit["sources"].append(dict(id="failed-read", url=failure["url"], content_sha256=empty, attempt_records=[
            dict(status="failed", elapsed_seconds=2, reserved_seconds=10, error="TLS")]))
        (self.root / "evidence" / "failed-read.txt").write_bytes(b"")
        self.proof["source_attempts"].append(dict(operation_id=failed_id, source_id="failed-read", attempt_index=0))
        self.manifest["added_evidence"].append(dict(operation_id=failed_id, archive_sha256="b"*64, file_sha256=failure_sha))
        self.payload["sources"].append(dict(operation_id=failed_id, result_sha256=failure_sha, body_sha256=empty, status="failed"))
        self.refresh_manifest()
        self.assertTrue(self.validate_only_continuation())
        row["artifacts"]["response.body.bin"] = dict(body, bytes=4)
        self.refresh_prior_input()
        with self.assertRaisesRegex(BriefError, "original settled research source"):
            self.validate_only_continuation()

    def test_successful_source_cannot_reuse_cancelled_grant(self):
        self.continuation()
        self.audit["sources"][1]["attempt_records"].append(dict(status="not_dispatched",
            operation_id=self.proof["source_attempts"][0]["operation_id"]))
        with self.assertRaisesRegex(BriefError, "reuse an undispatched"):
            self.validate_only_continuation()

    def test_known_failed_result_omission_still_requires_settlement(self):
        self.continuation()
        attempt=self.call["attempt_records"][-1]
        result=json.loads(self.root.joinpath("stage_artifacts",attempt["result_sha256"]+".json").read_bytes())
        result.update(status="timeout",error="Timeout")
        self.current_settle[0]["args"]["file_sha256"]=self.js(result)
        self.refresh_control(self.current_settle)
        attempt.update(status="timeout",error="Timeout",result_sha256=None)
        self.refresh_proof()
        self.validate_only_continuation()
        self.proof["control_commands"].remove(self.current_settle[2]); self.refresh_proof()
        with self.assertRaisesRegex(BriefError,"requires its original settlement"):
            self.validate_only_continuation()

    def test_research_with_editor_timeout_keeps_structured_blockers(self):
        self.continuation()
        editor=next(call for call in self.audit["model_calls"] if call["stage"]=="editor")
        editor.update(status="timeout",error="Timeout")
        editor["attempt_records"][-1].update(status="timeout",error="Timeout",result_sha256=None)
        self.draft=None
        result=self.check()
        self.assertIn("model_stage_failed",result["blockers"])
        self.assertIn("draft_missing",result["blockers"])
        self.assertIn("budget_accounting",result)

    def test_editor_retry_binds_actual_input_even_when_candidate_items_unchanged(self):
        self.continuation("editor")
        research=next(call for call in self.audit["model_calls"] if call["stage"]=="research")
        current=deepcopy(research["attempt_records"][0])
        current.update(prepublication_continuation_sha256="f"*64,
                       result_sha256=self.js(dict(outputs=[dict(sha256=self.artifact(encoded(self.candidates)))],new_result=True)))
        research["attempt_records"].append(current)
        self.save()
        read=lambda relative,limit:self.root.joinpath(relative).read_bytes()
        with self.assertRaisesRegex(BriefError,"must consume latest research"):
            validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        self.payload["research"]=dict(result_sha256=current["result_sha256"],outputs=[dict(sha256=self.artifact(encoded(self.candidates)))])
        self.refresh_manifest();self.save()
        validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        failed=self.call["attempt_records"][-1]
        retry=deepcopy(failed)
        failed.update(status="failed",error="Temporary failure")
        retry.pop("prepublication_continuation_sha256")
        retry["input_sha256"]=self.js(dict(research=dict(result_sha256=research["attempt_records"][0]["result_sha256"])))
        self.call["attempt_records"].append(retry)
        with self.assertRaisesRegex(BriefError,"must consume latest research"):
            validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        retry["input_sha256"]=self.js(dict(research=dict(result_sha256=current["result_sha256"],outputs=[dict(sha256=self.artifact(encoded(self.candidates)))])))
        validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        retry["input_sha256"]=failed["input_sha256"]
        validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        for supplied in (None, [], [dict(sha256="f"*64)]):
            claim=dict(result_sha256=current["result_sha256"],outputs=supplied)
            retry["input_sha256"]=self.js(dict(research=claim))
            with self.assertRaisesRegex(BriefError,"research outputs"):
                validate_current_outputs(self.audit,self.candidates,read,_require,_json)
        claim=dict(result_sha256=current["result_sha256"],outputs=[dict(sha256=self.artifact(encoded(self.candidates)))],status="timeout")
        retry["input_sha256"]=self.js(dict(research=claim))
        with self.assertRaisesRegex(BriefError,"research claims"):
            validate_current_outputs(self.audit,self.candidates,read,_require,_json)

    def test_original_grant_url_uses_canonical_identity_without_rewriting(self):
        self.continuation()
        op=self.source_reserve[0]["args"]["operations"][0]
        op["input"]["url"]=op["input"]["url"].replace("https://", "HTTPS://")
        self.refresh_control(self.source_reserve); self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")
        op["input"]["method"]="POST"
        self.refresh_control(self.source_reserve); self.refresh_proof()
        self.reject("request structure")

    def test_current_unknown_original_settlement_retains_blocked_accounting(self):
        self.continuation()
        attempt=self.call["attempt_records"][-1]
        attempt.update(status="unknown",elapsed_seconds=None,result_sha256=None,error="Unknown outcome",timing_unavailable_reason="No timing")
        self.call.update(status="unknown",elapsed_seconds=None,error="Unknown outcome",timing_unavailable_reason="No timing")
        self.current_settle[0]["args"].update(outcome="unknown",elapsed_seconds=None,file_sha256=None,archive_sha256=None)
        self.refresh_control(self.current_settle);self.refresh_proof()
        result=self.check()
        self.assertIn("operation_outcome_unknown",result["blockers"])
        self.assertIn("budget_accounting",result)

    def test_added_research_optional_claims_match_settled_result(self):
        for field,value in (("input_sha256","f"*64),("status","timeout"),("elapsed_seconds",99999)):
            with self.subTest(field=field):
                self.setUp();self.editor_research_continuation()
                self.payload["research"][field]=value
                self.refresh_manifest()
                with self.assertRaisesRegex(BriefError,"research claims"):
                    self.validate_only_continuation()

    def test_exact_candidates_allow_non_json_sidecar_outputs(self):
        self.continuation()
        attempt=self.call["attempt_records"][-1]
        result=json.loads(self.root.joinpath("stage_artifacts",attempt["result_sha256"]+".json").read_bytes())
        result["outputs"].append(dict(sha256=self.artifact(b"[Source](https://example.org) citation notes")))
        attempt["result_sha256"]=self.js(result)
        self.current_settle[0]["args"]["file_sha256"]=attempt["result_sha256"]
        self.refresh_control(self.current_settle);self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")
        result["outputs"][0]["sha256"]=self.artifact(encoded(dict(candidates=self.candidates)))
        attempt["result_sha256"]=self.js(result)
        self.current_settle[0]["args"]["file_sha256"]=attempt["result_sha256"]
        self.refresh_control(self.current_settle);self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")

    def test_current_epoch_reclaim_preserves_historical_controls(self):
        self.continuation()
        for triple in (self.current_reserve,self.current_settle):
            triple[0]["epoch"]=2
            self.refresh_control(triple)
        self.refresh_proof()
        self.assertEqual(self.check()["readiness"],"ready")
