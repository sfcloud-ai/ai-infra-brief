"""校验首发前增量模型调用的原始控制证据，不替代发布端权威许可。"""

import hashlib
import json
import math
import re

from .storage import BriefError
from .source_identity import source_identity, UnsupportedURL


MAX_BYTES = 20_000_000
SHA = re.compile(r"[a-f0-9]{64}")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")


def settled_research_input(document, request, blob, need):
    """以冻结输入引用的原始结果和结算命令验证嵌套来源清单。"""
    rows = document.get("sources")
    if not isinstance(rows, list) or not any(isinstance(row, dict) and "raw_result" in row for row in rows):
        return document

    def check(ok):
        need(ok, "original settled research source differs")

    def original(ref):
        check(isinstance(ref, dict) and isinstance(ref.get("sha256"), str) and SHA.fullmatch(ref["sha256"])
              and type(ref.get("bytes")) is int and 0 < ref["bytes"] <= MAX_BYTES)
        raw = blob(ref["sha256"])
        check(len(raw) == ref["bytes"] and hashlib.sha256(raw).hexdigest() == ref["sha256"])
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        check(isinstance(value, dict))
        return value

    sources, seen = [], set()
    for row in rows:
        check(isinstance(row, dict) and isinstance(row.get("settlement"), dict)
              and isinstance(row.get("artifacts"), dict))
        receipt = original(row.get("raw_result"))
        settlement = row["settlement"]
        command = original(settlement.get("original_command"))
        args = command.get("args")
        oid = row.get("operation_id")
        body = row["artifacts"].get("response.body.bin")
        raw_body = receipt.get("artifacts", {}).get("response.body.bin") if isinstance(receipt.get("artifacts"), dict) else None
        try:
            identity = source_identity(row.get("url"))["hash"]
        except UnsupportedURL:
            identity = None
        check(isinstance(oid, str) and UUID.fullmatch(oid) and oid not in seen and receipt.get("request_id") == request
              and all(key in row and key in receipt and row[key] == receipt[key]
                      for key in ("operation_id", "source_id", "url", "input_sha256", "status", "elapsed_seconds"))
              and row["status"] in ("ok", "failed") and type(row["elapsed_seconds"]) in (int, float)
              and math.isfinite(row["elapsed_seconds"]) and row["elapsed_seconds"] >= 0
              and type(receipt["elapsed_seconds"]) in (int, float) and isinstance(row["source_id"], str) and bool(row["source_id"])
              and identity is not None and row["input_sha256"] == identity
              and isinstance(body, dict) and isinstance(raw_body, dict) and isinstance(body.get("sha256"), str)
              and SHA.fullmatch(body["sha256"]) and body["sha256"] == raw_body.get("sha256") == receipt.get("content_sha256")
              and type(body.get("bytes")) is int and body["bytes"] >= (1 if row["status"] == "ok" else 0)
              and type(raw_body.get("bytes")) is int and body["bytes"] == raw_body["bytes"]
              and isinstance(command.get("command_id"), str) and UUID.fullmatch(command["command_id"])
              and command.get("action") == "settle" and command.get("request_id") == request and isinstance(args, dict)
              and args.get("operation_id") == oid and args.get("outcome") == "acknowledged"
              and type(args.get("elapsed_seconds")) in (int, float) and args["elapsed_seconds"] == row["elapsed_seconds"]
              and isinstance(settlement.get("archive_sha256"), str) and SHA.fullmatch(settlement["archive_sha256"])
              and args.get("archive_sha256") == settlement["archive_sha256"]
              and args.get("file_sha256") == settlement.get("result_sha256") == row["raw_result"]["sha256"])
        seen.add(oid)
        sources.append(dict(operation_id=oid, input_sha256=row["input_sha256"], result_sha256=args["file_sha256"],
                            url=row["url"], status=row["status"], elapsed_seconds=row["elapsed_seconds"], body_sha256=body["sha256"]))
    return {**document, "sources": sources}


def referenced_editor_input(document, request, previous_id, obj, need):
    """从首稿实际消费的研究产物解析来源。只按摘要取回原件。"""
    need(document.get("stage") == "editor" and document.get("request_id") == request
         and document.get("operation_id") == previous_id
         and document.get("source_records_location") == "findings.json#/source_records",
         "original editor source reference differs")
    research = document.get("research")
    need(isinstance(research, dict), "original research descriptor is required")
    result = obj(research.get("result_sha256"))
    outputs = research.get("outputs")
    need(result.get("stage") == "research" and result.get("request_id") == request
         and isinstance(research.get("operation_id"), str) and UUID.fullmatch(research["operation_id"])
         and isinstance(research.get("input_sha256"), str) and SHA.fullmatch(research["input_sha256"])
         and result.get("operation_id") == research.get("operation_id")
         and result.get("input_sha256") == research.get("input_sha256")
         and research.get("status") == result.get("stage_status", result.get("status"))
         and type(research.get("elapsed_seconds")) in (int, float) and research["elapsed_seconds"] == result.get("elapsed_seconds")
         and isinstance(outputs, list) and isinstance(result.get("outputs"), list),
         "original research result differs")
    def findings_hash(items):
        matches = [item.get("sha256") for item in items if isinstance(item, dict) and item.get("name") == "findings.json"]
        need(len(matches) == 1 and isinstance(matches[0], str) and SHA.fullmatch(matches[0]), "original findings descriptor differs")
        return matches[0]
    digest = findings_hash(outputs)
    need(digest == findings_hash(result["outputs"]), "original findings digest differs")
    findings = obj(digest)
    records = findings.get("source_records")
    need(all(result.get(k) == findings.get(k) for k in ("request_id", "operation_id", "input_sha256", "stage"))
         and isinstance(records, dict) and records and type(document.get("source_attempt_count")) is int
         and len(records) == document["source_attempt_count"], "original findings source inventory differs")
    sources = []
    for record in records.values():
        need(isinstance(record, dict) and isinstance(record.get("operation_id"), str) and UUID.fullmatch(record["operation_id"])
             and all(isinstance(record.get(k), str) and SHA.fullmatch(record[k])
                     for k in ("input_sha256", "result_sha256", "body_sha256")), "original findings source binding differs")
        sources.append({k: record[k] for k in ("operation_id", "input_sha256", "result_sha256", "body_sha256")})
    need(len({item["operation_id"] for item in sources}) == len(sources), "original findings repeat a source operation")
    telemetry = {"started_at", "finished_at", "stage_start", "input_read_ledger"}
    return {**document, "sources": sources, "research": {k: v for k, v in research.items() if k not in telemetry}}


def validate_continuation(metadata, record, attempt, previous, read, require, parse, source_bindings=None, *, expected_plan_hash=None):
    """只为有完整原始命令链、检查点和新增内容的同阶段续接放行。"""
    request = "morning-" + metadata["date"].replace("-", "") + "-r1"
    stage = record["stage"]

    def need(ok, message):
        require(ok, "Model continuation " + message)

    def exact(value, fields):
        return isinstance(value, dict) and set(value) == set(fields.split())

    def sha(value):
        return isinstance(value, str) and SHA.fullmatch(value) is not None

    def model_status(value):
        status = value.get("stage_status", value.get("status"))
        return "ok" if status == "success" else status

    def uid(value):
        return isinstance(value, str) and UUID.fullmatch(value) is not None

    def integer(value):
        return type(value) is int and value > 0

    def blob(value):
        need(sha(value), "artifact identity is invalid")
        raw = read(f"stage_artifacts/{value}.json", MAX_BYTES)
        need(hashlib.sha256(raw).hexdigest() == value, "artifact fingerprint mismatch")
        return raw

    def obj(value):
        try:
            result = parse(blob(value), "model continuation")
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise BriefError("Model continuation artifact is not JSON") from exc
        need(isinstance(result, dict), "artifact must be an object")
        return result

    proof = obj(attempt["prepublication_continuation_sha256"])
    need(exact(proof, "schema_version operation_id control_commands source_attempts")
         and proof["schema_version"] == "model_prepublication_continuation_evidence_v1"
         and uid(proof["operation_id"]), "proof schema mismatch")
    cancelled_ids = {old.get("operation_id") for source in metadata["sources"]
                     for old in source.get("attempt_records", []) if old["status"] == "not_dispatched"}
    source_links = proof["source_attempts"]
    need(isinstance(source_links, list) and len(source_links) <= 64, "source attempt mappings must be bounded")
    linked, pointers = {}, set()
    bound_operations, bound_attempts = source_bindings if source_bindings is not None else ({}, {})
    for link in source_links:
        need(exact(link, "operation_id source_id attempt_index") and uid(link["operation_id"])
             and isinstance(link["source_id"], str) and type(link["attempt_index"]) is int
             and link["attempt_index"] >= 0, "source attempt mapping fields are invalid")
        pointer = (link["source_id"], link["attempt_index"])
        need(link["operation_id"] not in linked and pointer not in pointers, "source attempt mappings must be unique")
        # 同一读取可被两阶段复用，但不同读取不能共用一次计费记录。
        need(bound_operations.get(link["operation_id"], pointer) == pointer
             and bound_attempts.get(pointer, link["operation_id"]) == link["operation_id"],
             "source attempt mappings must be unique across continuation proofs")
        bound_operations[link["operation_id"]] = pointer
        bound_attempts[pointer] = link["operation_id"]
        linked[link["operation_id"]] = pointer
        pointers.add(pointer)
    pairs = proof["control_commands"]
    need(isinstance(pairs, list) and 3 <= len(pairs) <= 264, "bounded control evidence is required")
    controls, reserves, settlements, dispatches = set(), {}, {}, {}
    resumptions = []
    revisions = set()
    plan_hash = expected_plan_hash
    for pair in pairs:
        need(exact(pair, "command_sha256 receipt_sha256"), "control reference fields mismatch")
        command, envelope = obj(pair["command_sha256"]), obj(pair["receipt_sha256"])
        need(envelope.get("type") == "control_observed" and isinstance(envelope.get("result"), dict),
             "original control receipt wrapper is required")
        receipt = envelope["result"]
        run = receipt.get("run")
        cid = command.get("command_id")
        need(uid(cid) and cid not in controls
             and command.get("schema_version") == "research_control_v2"
             and command.get("request_id") == request
             and receipt.get("command_id") == cid and receipt.get("request_id") == request
             and receipt.get("command_sha256") == pair["command_sha256"]
             and receipt.get("status") == "committed" and not receipt.get("replayed")
             and (not receipt.get("grant_expired") or command.get("action") in {"reserve", "resume"}
                  and receipt.get("dispatch_granted") is False) and isinstance(run, dict)
             and run.get("request_id") == request and "permit" in run and run["permit"] is None
             and integer(run.get("revision"))
             and type(command.get("expected_revision")) is int
             and run["revision"] == command["expected_revision"] + 1
             and sha(run.get("plan_hash")), "control command/receipt binding mismatch")
        if plan_hash is None:
            plan_hash = run["plan_hash"]
        need(run["plan_hash"] == plan_hash, "commands must retain the same plan")
        need(run["revision"] not in revisions, "distinct commands cannot share a committed revision")
        revisions.add(run["revision"])
        controls.add(cid)
        args = command.get("args")
        need(isinstance(args, dict), "command arguments are missing")
        if command.get("action") == "reserve":
            operations = args.get("operations")
            grants = receipt.get("operations")
            need(type(receipt.get("dispatch_granted")) is bool and isinstance(operations, list)
                 and isinstance(grants, list) and len(operations) == len(grants), "reserve receipt is required")
            for position, operation in enumerate(operations):
                need(isinstance(operation, dict) and uid(operation.get("operation_id"))
                     and operation.get("kind") in ("source", "model", "native") and isinstance(operation.get("key"), str)
                     and sha(operation.get("input_sha256")) and "input" in operation
                     and (operation["input"] is None or isinstance(operation["input"], dict))
                     and type(operation.get("max_seconds")) in {int, float}
                     and (operation["max_seconds"] == 0 if operation["kind"] == "native" else 0 < operation["max_seconds"] <= 3600), "reserve operation is invalid")
                oid = operation["operation_id"]
                matches = [g for g in grants if isinstance(g, dict) and g.get("operation_id") == oid]
                need(oid not in reserves and len(matches) == 1
                     and all(matches[0].get(k) == operation.get(k) for k in ("kind", "key", "input_sha256")),
                     "grant operation binding mismatch")
                reserves[oid] = (operation, (run["revision"], position))
                dispatches.setdefault(oid, []).append((run["revision"], receipt["dispatch_granted"]))
        elif command.get("action") == "resume":
            grants = receipt.get("operations")
            need(exact(args, "operation_id") and uid(args["operation_id"])
                 and type(receipt.get("dispatch_granted")) is bool and isinstance(grants, list)
                 and len(grants) == 1 and isinstance(grants[0], dict)
                 and grants[0].get("operation_id") == args["operation_id"], "resume grant binding mismatch")
            resumptions.append((args["operation_id"], grants[0], run["revision"], receipt["dispatch_granted"]))
        elif command.get("action") == "settle":
            oid = args.get("operation_id")
            known_settle = (args.get("outcome") == "acknowledged"
                            and type(args.get("elapsed_seconds")) in {int, float} and args["elapsed_seconds"] >= 0)
            unknown_current = (oid == proof["operation_id"] and args.get("outcome") == "unknown"
                               and args.get("elapsed_seconds") is None and attempt["elapsed_seconds"] is None
                               and args.get("file_sha256") == attempt["result_sha256"])
            need(uid(oid) and oid not in settlements and (known_settle or unknown_current)
                 and receipt.get("dispatch_granted") is False, "known acknowledged settlement is required")
            settlements[oid] = (args, run["revision"])
        else:
            need(False, "only original reserve, resume and settle commands belong in proof")

    # 恢复沿用原预留；新授权只改变可执行时序，不增加尝试次数或预算。
    for oid, grant, revision, fresh in resumptions:
        need(oid in reserves, "resume requires its original reservation")
        operation, reserved_revision = reserves[oid]
        need(reserved_revision[0] < revision
             and (oid not in settlements or revision < settlements[oid][1])
             and all(grant.get(k) == operation.get(k) for k in ("kind", "key", "input_sha256")),
             "resume must bind its original operation before settlement")
        dispatches.setdefault(oid, []).append((revision, fresh))

    def dispatch_revision(oid):
        revision, fresh = max(dispatches.get(oid, [(0, False)]))
        need(fresh, "fresh reserve or resume grant is required")
        return revision

    manifest = obj(attempt["input_sha256"])
    need(exact(manifest, "schema_version request_id stage previous_operation_id checkpoint_archive_sha256 checkpoint_file_sha256 payload_sha256 added_evidence")
         and manifest["schema_version"] == "model_prepublication_continuation_v1"
         and manifest["request_id"] == request and manifest["stage"] == stage,
         "manifest schema or stage mismatch")
    previous_id = manifest["previous_operation_id"]
    current_id = proof["operation_id"]
    need(uid(previous_id) and current_id != previous_id and current_id in reserves and previous_id in reserves,
         "prior and current grants are required")
    current_op, current_revision = reserves[current_id]
    prior_op, prior_revision = reserves[previous_id]
    dispatch_revision(current_id)
    expected_input = {k: manifest[k] for k in ("previous_operation_id", "checkpoint_archive_sha256", "checkpoint_file_sha256")}
    current_input = current_op.get("input")
    need(isinstance(current_input, dict) and set(current_input) == set(expected_input) | {"continuation_archive_sha256"}
         and all(current_input[k] == value for k, value in expected_input.items())
         and sha(current_input["continuation_archive_sha256"])
         and current_op.get("kind") == prior_op.get("kind") == "model"
         and current_op.get("key") == prior_op.get("key") == stage
         and current_op.get("input_sha256") == attempt["input_sha256"]
         and attempt["input_sha256"] != previous["input_sha256"]
         and prior_op.get("input_sha256") == previous["input_sha256"]
         and current_op.get("max_seconds") == attempt["reserved_seconds"]
         and prior_op.get("max_seconds") == previous["reserved_seconds"]
         and prior_revision < current_revision, "model reserve/input binding mismatch")
    need(previous["status"] in {"ok", "partial"} and previous["elapsed_seconds"] is not None
         and manifest["checkpoint_file_sha256"] == previous["result_sha256"] == attempt["checkpoint_sha256"]
         and sha(manifest["checkpoint_archive_sha256"]), "preceding successful checkpoint must be retained")
    payload, original = obj(manifest["payload_sha256"]), obj(previous["input_sha256"])
    prior_input = original
    if stage == "research":
        prior_input = settled_research_input(original, request, blob, need)
    if "sources" not in original and stage == "editor" and original.get("source_records_location") == "findings.json#/source_records":
        prior_input = referenced_editor_input(original, request, previous_id, obj, need)
        if payload.get("research") == original["research"]:
            payload = {**payload, "research": prior_input["research"]}
    source_fields = set("batch operation_id input_sha256 url status elapsed_seconds result_file result_sha256 body_file body_sha256 source_label".split())
    research_fields = set("operation_id input_file input_sha256 result_file result_sha256 status elapsed_seconds outputs".split())
    output_fields = {"name", "path", "sha256", "bytes"}
    for document in (payload, prior_input):
        need(document.get("request_id") == request and document.get("stage") == stage
             and isinstance(document.get("sources"), list)
             and all(isinstance(source, dict) and set(source) <= source_fields and "body_sha256" in source
                     and (sha(source["body_sha256"]) or source["body_sha256"] is None
                          and source.get("body_file") is None and source.get("status") not in ("ok", "success"))
                     for source in document["sources"]),
             "payload and original input must retain the stage input structure")
        research_input = document.get("research")
        need("research" not in document or isinstance(research_input, dict)
             and set(research_input) <= research_fields and "outputs" in research_input,
             "research input structure is invalid")
        input_outputs = [] if research_input is None else research_input["outputs"]
        need(isinstance(input_outputs, list) and all(isinstance(output, dict) and set(output) <= output_fields
             and sha(output.get("sha256")) for output in input_outputs), "research input outputs are invalid")
    binding = payload.get("continuation")
    need(exact(binding, "previous_operation_id checkpoint_archive_sha256 checkpoint_file_sha256 added_evidence")
         and all(binding[k] == manifest[k] for k in binding)
         and manifest["payload_sha256"] != previous["input_sha256"], "payload checkpoint and evidence binding mismatch")
    prior_hashes, pending = set(), [original, prior_input] if prior_input is not original else [original]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif sha(value):
            prior_hashes.add(value)

    def settled(oid, archive_sha, file_sha, before_revision):
        need(oid in settlements and oid in reserves, "result grant and settlement are required")
        settlement, revision = settlements[oid]
        operation, reserved_revision = reserves[oid]
        need(reserved_revision[0] <= dispatch_revision(oid) < revision < before_revision[0]
             and settlement.get("archive_sha256") == archive_sha
             and settlement.get("file_sha256") == file_sha and sha(archive_sha),
             "result archive/settlement order mismatch")
        result = obj(file_sha)
        need(result.get("request_id") == request and result.get("operation_id") == oid
             and result.get("input_sha256") == operation["input_sha256"]
             and type(result.get("elapsed_seconds")) in {int, float}
             and result.get("elapsed_seconds") == settlement["elapsed_seconds"],
             "settled result identity or timing mismatch")
        return result

    prior_result = settled(previous_id, manifest["checkpoint_archive_sha256"],
                           manifest["checkpoint_file_sha256"], current_revision)
    need(prior_result.get("stage") == stage and model_status(prior_result) == previous["status"]
         and prior_result.get("elapsed_seconds") == previous["elapsed_seconds"]
         and prior_result.get("error", "") == previous["error"], "prior result differs from retained attempt")
    known = attempt["elapsed_seconds"] is not None and attempt["status"] != "unknown"
    current_file = attempt["result_sha256"]
    if known:
        need(current_id in settlements, "current known result requires its original settlement")
        current_settlement, settled_revision = settlements[current_id]
        if current_file is None:
            current_file = current_settlement.get("file_sha256")
        settled(current_id, current_settlement.get("archive_sha256"), current_file, (settled_revision + 1, 0))
    elif current_id in settlements:
        current_settlement, settled_revision = settlements[current_id]
        need(current_revision[0] < settled_revision and current_settlement["outcome"] == "unknown"
             and current_settlement["elapsed_seconds"] is None and current_settlement.get("file_sha256") == current_file,
             "unresolved current settlement differs from retained attempt")
    if current_file is not None:
        current_result = obj(current_file)
        need(current_result.get("request_id") == request and current_result.get("operation_id") == current_id
             and current_result.get("stage") == stage and current_result.get("input_sha256") == attempt["input_sha256"]
             and model_status(current_result) == attempt["status"]
             and (current_result.get("elapsed_seconds") is None or type(current_result.get("elapsed_seconds")) in {int, float})
             and current_result.get("elapsed_seconds") == attempt["elapsed_seconds"]
             and current_result.get("error", "") == attempt["error"], "current result differs from retained attempt")
    evidence = manifest["added_evidence"]
    need(isinstance(evidence, list) and 1 <= len(evidence) <= 64, "new evidence list is required")
    seen, used_links, added_sources = set(), set(), set()
    added_research = False
    new_success = False
    for item in evidence:
        need(exact(item, "operation_id archive_sha256 file_sha256") and uid(item["operation_id"])
             and item["operation_id"] not in seen and item["operation_id"] in reserves,
             "added evidence identities must be unique and granted")
        oid = item["operation_id"]
        seen.add(oid)
        operation, revision = reserves[oid]
        need(prior_revision < revision < current_revision, "evidence must be reserved after the prior model")
        result = settled(oid, item["archive_sha256"], item["file_sha256"], current_revision)
        if operation.get("kind") == "source":
            need(oid not in cancelled_ids, "executed source cannot reuse an undispatched operation")
            added_sources.add(oid)
            content_sha = result.get("content_sha256")
            failed_context = stage == "research" and prior_input is not original and result.get("status") == "failed"
            if failed_context:
                need(result.get("dispatch_started") is True and type(result.get("attempts_in_this_operation")) is int
                     and result["attempts_in_this_operation"] == 1 and isinstance(result.get("error"), str) and result["error"].strip()
                     and type(result.get("curl_exit_code")) is int and (result["curl_exit_code"] != 0
                     or type(result.get("http_status")) is int and not 200 <= result["http_status"] < 300)
                     and sha(content_sha), "failed context must bind an executed GET failure")
            else:
                need(result.get("status") in ("ok", "success") and result.get("response_complete") is True
                 and result.get("curl_exit_code") == 0 and type(result.get("curl_exit_code")) is int
                 and result.get("error") == "" and type(result.get("http_status")) is int
                 and 200 <= result["http_status"] < 300 and sha(content_sha), "source evidence must be a complete successful response")
            need(oid in linked, "source operation must map to a retained attempt")
            source_id, attempt_index = linked[oid]
            inputs = [source for source in payload["sources"] if source.get("operation_id") == oid]
            need(len(inputs) == 1 and inputs[0].get("result_sha256") == item["file_sha256"]
                 and inputs[0].get("body_sha256") == content_sha,
                 "payload source must bind the actual result and response")
            source_claim = inputs[0]
            need(not failed_context or source_claim.get("status") == "failed", "failed context must retain failed status")
            claimed_identity = None
            if "url" in source_claim:
                try:
                    claimed_identity = source_identity(source_claim["url"])["hash"]
                except (UnsupportedURL, TypeError) as exc:
                    raise BriefError("Model continuation payload source identity is invalid") from exc
            need(("url" not in source_claim or claimed_identity == operation["input_sha256"])
                 and ("input_sha256" not in source_claim or source_claim["input_sha256"] == operation["input_sha256"]),
                 "payload source identity differs from the original request")
            need(("status" not in source_claim or source_claim["status"] == result["status"])
                 and ("elapsed_seconds" not in source_claim or type(source_claim["elapsed_seconds"]) in {int, float}
                      and source_claim["elapsed_seconds"] == result["elapsed_seconds"]),
                 "payload source outcome differs from its settled result")
            need(failed_context or content_sha not in prior_hashes, "added source must contain new response bytes")
            matching = [source for source in metadata["sources"] if source["id"] == source_id]
            need(len(matching) == 1 and attempt_index < len(matching[0].get("attempt_records", [])),
                 "source operation must map to exactly one retained attempt")
            source = matching[0]
            source_attempt = source["attempt_records"][attempt_index]
            try:
                identity = source_identity(source["url"])
                result_identity = source_identity(result.get("url"))
                grant_input = operation.get("input")
                need(isinstance(grant_input, dict) and set(grant_input) == {"method", "url"}
                     and grant_input["method"] == "GET", "source request structure mismatch")
                grant_identity = source_identity(grant_input["url"])
            except (UnsupportedURL, TypeError) as exc:
                raise BriefError("Model continuation source request identity is invalid") from exc
            need(operation.get("input_sha256") == operation.get("key") == identity["hash"]
                 == result_identity["hash"] == grant_identity["hash"],
                 "source request fingerprint mismatch")
            used_links.add(oid)
            need(source_attempt["status"] == ("failed" if failed_context else "ok") and source_attempt["error"] == result["error"]
                 and source_attempt["elapsed_seconds"] == result["elapsed_seconds"]
                 and source_attempt["reserved_seconds"] == operation["max_seconds"], "source attempt binding mismatch")
            executed = [index for index, old in enumerate(source["attempt_records"]) if old["status"] != "not_dispatched"]
            need(attempt_index != executed[-1] or source["content_sha256"] == content_sha,
                 "last executed source result must match current response bytes")
            path = source["id"] if source["content_sha256"] == content_sha else content_sha
            body = read(f"evidence/{path}.txt", MAX_BYTES)
            need(hashlib.sha256(body).hexdigest() == content_sha and (failed_context or bool(body.strip())), "source response bytes must match the retained outcome")
            new_success |= not failed_context
        else:
            added_research = True
            need(stage == "editor" and operation.get("kind") == "model" and operation.get("key") == "research"
                 and result.get("stage") == "research" and model_status(result) in ("ok", "partial"),
                 "only new research results may extend the editor")
            matches = [a for call in metadata["model_calls"] if call["stage"] == "research"
                       for a in call.get("attempt_records", []) if a.get("result_sha256") == item["file_sha256"]]
            need(len(matches) == 1 and matches[0]["status"] == model_status(result)
                 and matches[0]["elapsed_seconds"] == result["elapsed_seconds"]
                 and matches[0]["input_sha256"] == operation["input_sha256"]
                 and matches[0]["error"] == result.get("error", "")
                 and matches[0]["reserved_seconds"] == operation["max_seconds"], "research result must map to a retained attempt")
            available = set()
            if "outputs" in result:
                outputs = result["outputs"]
                need(isinstance(outputs, list) and all(isinstance(output, dict) and sha(output.get("sha256"))
                     for output in outputs), "declared research outputs are invalid")
                available.update(output["sha256"] for output in outputs)
            if "output_sha256" in result:
                need(sha(result["output_sha256"]), "declared research output fingerprint is invalid")
                available.add(result["output_sha256"])
            need(bool(available), "research output bytes are required")
            need(all(bool(blob(output_sha).strip()) for output_sha in available),
                 "research output bytes must be retained and nonempty")
            research = payload.get("research")
            need(isinstance(research, dict) and research.get("operation_id") == oid
                 and research.get("result_sha256") == item["file_sha256"]
                 and isinstance(research.get("outputs"), list), "payload research result binding mismatch")
            need(("input_sha256" not in research or research["input_sha256"] == operation["input_sha256"])
                 and ("status" not in research or research["status"] == result.get("stage_status", result.get("status")))
                 and ("elapsed_seconds" not in research or type(research["elapsed_seconds"]) in {int, float}
                      and research["elapsed_seconds"] == result["elapsed_seconds"]),
                 "payload research claims differ from the settled result")
            referenced = {entry["sha256"] for entry in research["outputs"]
                          if isinstance(entry, dict) and sha(entry.get("sha256"))}
            need(available == referenced, "payload must reference exactly the actual research outputs")
            need(bool(available - prior_hashes), "added research must contain new output bytes")
    def same(left, right):
        if isinstance(left, dict):
            return isinstance(right, dict) and set(left) == set(right) and all(same(left[k], right[k]) for k in left)
        if isinstance(left, list):
            return isinstance(right, list) and len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
        if isinstance(left, bool) or isinstance(right, bool):
            return type(left) is type(right) and left == right
        return left == right

    need(all((isinstance(source.get("operation_id"), str) and source["operation_id"] in added_sources)
             or any(same(source, prior) for prior in prior_input["sources"]) for source in payload["sources"]),
         "payload contains an unverified new source reference")
    need("research" not in payload or added_research
         or "research" in prior_input and same(payload["research"], prior_input["research"]),
         "payload contains an unverified new research reference")
    mutable_fields = {"request_id", "stage", "sources", "research", "continuation"}
    need(all(key in mutable_fields or key in prior_input and same(value, prior_input[key])
             for key, value in payload.items()), "payload contains unverified top-level context")
    need(new_success or added_research, "a continuation needs new successful evidence")
    need(used_links == set(linked), "unused source attempt mappings are not allowed")
    return True


def validate_current_outputs(metadata, candidates, read, require, parse):
    """同一首发轮次保留续接输出约束，后续已认证页面另起轮次。"""
    calls = {call["stage"]: call for call in metadata["model_calls"]}
    latest, chains = {}, {}
    for stage, call in calls.items():
        attempts = call.get("attempt_records", [])
        if not attempts:
            continue
        latest[stage] = attempts[-1]
        round_id = attempts[-1].get("page_round", {}).get("revision", 1)
        chained = [(index, attempt) for index, attempt in enumerate(attempts)
                   if attempt.get("page_round", {}).get("revision", 1) == round_id
                   and "prepublication_continuation_sha256" in attempt]
        if chained and attempts[-1]["status"] in ("ok", "partial"):
            chains[stage] = chained[-1]
    if not chains:
        return

    def artifact(digest):
        require(isinstance(digest, str) and SHA.fullmatch(digest), "Continuation output digest is required")
        raw = read(f"stage_artifacts/{digest}.json", MAX_BYTES)
        require(hashlib.sha256(raw).hexdigest() == digest, "Continuation output fingerprint mismatch")
        return raw

    def obj(digest):
        value = parse(artifact(digest), "continuation output")
        require(isinstance(value, dict), "Continuation output must be an object")
        return value

    def outputs(attempt):
        result = obj(attempt["result_sha256"])
        declared = result.get("outputs", [])
        require(isinstance(declared, list) and all(isinstance(item, dict) and isinstance(item.get("sha256"), str)
                and SHA.fullmatch(item["sha256"]) for item in declared), "Continuation declared outputs are invalid")
        hashes = {item["sha256"] for item in declared}
        if "output_sha256" in result:
            digest = result["output_sha256"]
            require(isinstance(digest, str) and SHA.fullmatch(digest), "Continuation scalar output is invalid")
            hashes.add(digest)
        require(bool(hashes), "Continuation outputs must bind actual candidates or draft")
        return hashes

    def candidate_match(raw, allow_standalone):
        if raw.lstrip()[:1] not in (b"{", b"["):
            return None
        try:
            value = parse(raw, "continuation research output")
        except BriefError:
            return None
        if isinstance(value, dict) and ("candidates" in value or allow_standalone):
            value = value.get("candidates", value)
            if isinstance(value, dict) and value.get("mode") == "live" and isinstance(value.get("items"), list):
                return value["items"] == candidates["items"]
        return None

    def check_candidates(hashes, direct_hash=None, allow_standalone=False):
        matched = False
        consistent = True
        for digest in hashes:
            raw = artifact(digest)
            if direct_hash in hashes:
                if digest == direct_hash:
                    matched = raw == read("candidates.json", MAX_BYTES)
            else:
                match = candidate_match(raw, allow_standalone)
                if match is not None:
                    matched = True
                    consistent = consistent and match
        return matched and consistent

    def bind_draft(attempt):
        expected = metadata["inputs"]["draft_sha256"]
        declared = outputs(attempt)
        matches = False
        for digest in declared:
            raw = artifact(digest)
            if digest == expected:
                matches = raw == read("draft.json", MAX_BYTES)
        require(matches,
                "Continuation current output must bind final candidates or draft bytes")

    if "research" in chains:
        declared = outputs(latest["research"])
        expected = metadata["inputs"]["candidates_sha256"]
        require(check_candidates(declared, direct_hash=expected),
                "Continuation current output must bind final candidates or draft bytes")
        if "editor" in latest and latest["editor"]["status"] in ("ok", "partial"):
            bind_draft(latest["editor"])
    if "editor" in chains:
        bind_draft(latest["editor"])
        editor_index, editor_chain = chains["editor"]
        actual_input = obj(latest["editor"]["input_sha256"])
        is_manifest = ("prepublication_continuation_sha256" in latest["editor"]
                       or latest["editor"]["input_sha256"] == editor_chain["input_sha256"])
        payload = obj(actual_input["payload_sha256"]) if is_manifest else actual_input
        consumed = payload.get("research")
        if "research" in chains:
            require(isinstance(consumed, dict) and consumed.get("result_sha256") == latest["research"]["result_sha256"],
                    "Editor current input must consume latest research result")
            latest_research = latest["research"]
            latest_result = obj(latest_research["result_sha256"])
            require(("operation_id" not in consumed or consumed["operation_id"] == latest_result.get("operation_id"))
                    and ("input_sha256" not in consumed or consumed["input_sha256"] == latest_research["input_sha256"])
                    and ("status" not in consumed or consumed["status"] == latest_result.get("stage_status", latest_result.get("status")))
                    and ("elapsed_seconds" not in consumed or type(consumed["elapsed_seconds"]) in {int, float}
                         and consumed["elapsed_seconds"] == latest_research["elapsed_seconds"]),
                    "Editor current research claims must match latest result")
            consumed_outputs = consumed.get("outputs")
            require(isinstance(consumed_outputs, list) and all(isinstance(item, dict)
                    and isinstance(item.get("sha256"), str) and SHA.fullmatch(item["sha256"])
                    for item in consumed_outputs), "Editor current research outputs are required")
            require({item["sha256"] for item in consumed_outputs} == outputs(latest["research"]),
                    "Editor current research outputs must match latest result")
        else:
            require("research" in latest, "Editor-only continuation needs retained research")
            prior_attempt = calls["editor"]["attempt_records"][editor_index - 1]
            prior_input = obj(prior_attempt["input_sha256"])
            retained = prior_input.get("research")
            require(isinstance(retained, dict) and isinstance(retained.get("result_sha256"), str),
                    "Editor-only continuation needs its original research checkpoint")
            matching = [attempt for attempt in calls["research"].get("attempt_records", [])
                        if attempt.get("result_sha256") == retained["result_sha256"]]
            require(len(matching) == 1, "Editor prior checkpoint must match a retained research attempt")
            require(check_candidates(outputs(matching[0]), allow_standalone=True),
                    "Editor-only continuation cannot introduce new research candidates")
