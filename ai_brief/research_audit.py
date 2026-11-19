"""只读验证真实晨报的私有证据包，不联网、不发布、不修改投递状态。"""

from datetime import date
from decimal import Decimal, localcontext
import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .contracts import parse_time, validate
from .storage import BriefError
from .source_identity import source_identity
from .coverage import CONTRACT, REQUIRED_CHECKS, FAILURE_KINDS, source_matches, notice_prefix
from . import discovery as community
from .research_continuation import validate_continuation, validate_current_outputs


# 仅容纳新版真实计时求和的浮点误差，不修改预留声明、次数或原始耗时。
LAYERED_TIME_TOLERANCE = 1e-6
MAX_FILE_BYTES = 2_000_000
MAX_PACKAGE_BYTES = 20_000_000
WHOLE_EDITION_PACKAGE_BYTES = 32_000_000
SELECTED = {"selected", "update"}
PAGE_CHECKS = {
    "model-serving-gpu": ("model_serving",),
    "hardware-status": ("hardware",),
    "model-architecture-status": ("model_architecture", "glm_blog", "qwen_blog", "glm_models",
                                  "deepseek_models", "qwen_models", "deepseek_articles"),
    "embodied-training": ("embodied_training",),
    "small-model-status": ("small_models",),
    "inference-engines": ("inference_engines", "vllm", "sglang", "deepinfer",
                          "vllm_blog", "sglang_blog", "lmsys_blog", "deepinfer_articles"),
}
COVERAGE_LABELS = {
    "vllm": "vLLM 代码", "sglang": "SGLang 代码", "deepinfer": "DeepInfer 代码",
    "vllm_blog": "vLLM 博客", "sglang_blog": "SGLang 博客",
    "deepseek_articles": "DeepSeek 文章", "deepinfer_articles": "DeepInfer 文章",
}


def _json(data, label):
    # 重复键和非有限数不能在解析后静默覆盖真实记录。
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise BriefError(f"Invalid {label} JSON") from exc


def _require(condition, message):
    if not condition:
        raise BriefError(message)


def _settled_research(call, read):
    """读取同一次结算中的专题交接及覆盖快照，各自校验原件摘要。"""
    if not call or call["status"] not in {"ok", "partial"} or not call.get("attempt_records"):
        return {}, {}
    attempt = call["attempt_records"][-1]
    digest = attempt.get("result_sha256")
    if not digest:
        return {}, {}
    raw = read(f"stage_artifacts/{digest}.json", MAX_PACKAGE_BYTES)
    result = _json(raw, "research result")
    if not isinstance(result, dict) or not isinstance(result.get("outputs", []), list):
        return {}, {}
    matches = []
    for item in result.get("outputs", []):
        if not isinstance(item, dict):
            continue
        labels = [item[k].replace("\\", "/").rsplit("/", 1)[-1] for k in ("name", "path") if isinstance(item.get(k), str)]
        if "findings.json" in labels:
            _require(all(label == "findings.json" for label in labels), "Conflicting research findings descriptor")
            matches.append(item.get("sha256"))
    if not matches and result.get("output_sha256"):
        matches = [result["output_sha256"]]
    if not matches:
        return {}, {}
    status = result.get("stage_status", result.get("status"))
    _require(hashlib.sha256(raw).hexdigest() == digest and result.get("stage") == "research"
             and result.get("input_sha256") == attempt["input_sha256"]
             and ("ok" if status == "success" else status) == attempt["status"] == call["status"]
             and "elapsed_seconds" in result and result["elapsed_seconds"] == attempt["elapsed_seconds"]
             and len(matches) == 1 and isinstance(matches[0], str) and re.fullmatch(r"[a-f0-9]{64}", matches[0]),
             "Research findings must match the settled research attempt")
    data = read(f"stage_artifacts/{matches[0]}.json", MAX_PACKAGE_BYTES)
    _require(hashlib.sha256(data).hexdigest() == matches[0], "Research findings fingerprint mismatch")
    document = _json(data, "research findings")
    snapshot = document
    snapshot_sha = result.get("output_sha256")
    if snapshot_sha is not None and snapshot_sha != matches[0]:
        _require(isinstance(snapshot_sha, str) and re.fullmatch(r"[a-f0-9]{64}", snapshot_sha),
                 "Invalid settled research snapshot fingerprint")
        raw_snapshot = read(f"stage_artifacts/{snapshot_sha}.json", MAX_PACKAGE_BYTES)
        _require(hashlib.sha256(raw_snapshot).hexdigest() == snapshot_sha, "Research snapshot fingerprint mismatch")
        snapshot = _json(raw_snapshot, "research coverage snapshot")
    _require(isinstance(snapshot, dict), "Invalid settled research snapshot")
    findings = document
    if isinstance(findings, dict):
        findings = findings.get("findings", findings)
    _require(isinstance(findings, dict) and isinstance(findings.get("candidate_bindings_and_decisions", []), list)
             and isinstance(findings.get("page_status_sources", {}), dict)
             and all(page in PAGE_CHECKS and isinstance(items, list) for page, items in findings.get("page_status_sources", {}).items()),
             "Invalid research topic handoff")
    return findings, snapshot


def _assigned_primary_matches(assignment, findings, url, reads):
    source_id = assignment.get("primary_source_id")
    if not isinstance(source_id, str):
        return False
    records = findings.get("source_records", findings.get("sources", []))
    if isinstance(records, dict):
        records = [records[source_id]] if source_id in records else []
    elif isinstance(records, list):
        records = [r for r in records if isinstance(r, dict) and r.get("source_id", r.get("id")) == source_id]
    else:
        return False
    if "url" in assignment or "content_sha256" in assignment:
        records = [*records, assignment]
    return bool(records) and all(isinstance(r, dict) and r.get("url") == url
                                 and isinstance(r.get("content_sha256", r.get("body_sha256")), str)
                                 and (source_id, r.get("content_sha256", r.get("body_sha256"))) in reads for r in records)


def _time(value):
    # 共享时间解析器会携带原始值；私有审计必须在本层统一脱敏。
    try:
        return parse_time(value)
    except BriefError as exc:
        raise BriefError("Invalid audit timestamp") from exc


def _url(value):
    try:
        parts = urlsplit(value)
        parts.port
        valid = (parts.scheme in {"http", "https"} and parts.hostname
                 and not parts.username and not parts.password
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    _require(valid, "Source URL must be HTTP(S), with a hostname and no credentials")


def validate_audit(directory):
    """验证文件绑定及审计一致性；所有文件错误均隐去私有路径。"""
    try:
        return _validate_audit(directory)
    except (OSError, RuntimeError) as exc:
        raise BriefError("Cannot read private audit package") from exc


def _validate_audit(directory):
    """只读检查；通过不等于证实事实或允许公开私有材料。"""
    root = Path(directory).resolve(strict=True)
    total = 0
    package_limit = MAX_PACKAGE_BYTES
    read_cache = {}

    def read(relative, max_bytes=MAX_FILE_BYTES):
        nonlocal total
        if relative in read_cache:
            data = read_cache[relative]
            _require(len(data) <= max_bytes, "Audit file exceeds size limit")
            return data
        path = root / relative
        _require(not path.is_symlink() and not path.parent.is_symlink(), "Audit files must not be symlinks")
        _require(path.is_file(), f"Missing audit file: {relative}")
        _require(path.stat().st_size <= max_bytes, "Audit file exceeds size limit")
        remaining = min(max_bytes, package_limit - total)
        with path.open("rb") as stream:
            data = stream.read(remaining + 1)
        total += len(data)
        _require(len(data) <= max_bytes and total <= package_limit, "Audit package exceeds size limit")
        read_cache[relative] = data
        return data

    metadata_bytes = read("audit.json")
    metadata = _json(metadata_bytes, "audit")
    try:
        validate("research-audit", metadata)
    except BriefError as exc:
        # 不把完整候选、摘录或私人路径回显到运行日志。
        raise BriefError("Invalid research-audit metadata schema") from exc
    page_scope = metadata.get("page_scope")
    discovery_contract = metadata.get("coverage_contract") == community.CONTRACT
    required_checks = (*REQUIRED_CHECKS, *community.CHECKS) if discovery_contract else REQUIRED_CHECKS
    page_checks = {page: (*checks, *community.PAGE_CHECKS.get(page, ())) if discovery_contract else checks
                   for page, checks in PAGE_CHECKS.items()}
    ready_pages, pending_checks = [], set()
    previous_metadata = previous_report = previous_summary = None
    old_pages, current_revision = [], 1
    if page_scope is not None:
        _require(metadata.get("coverage_policy") == "publish_verified_with_gaps", "Page scope requires explicit partial coverage policy")
        delivery = page_scope["page_delivery"]
        added_pages = delivery["added"]
        previous = delivery["previous"]
        evidence = page_scope["previous_evidence"]
        _require((previous is None) == (evidence is None), "Previous page evidence is required exactly for successors")
        if previous is not None:
            _require(previous == {key: evidence[key] for key in ("request_id", "bundle_sha256")},
                     "Previous delivery and evidence binding differ")
            previous_summary_bytes = read("previous/audit.json")
            _require(hashlib.sha256(previous_summary_bytes).hexdigest() == evidence["audit_sha256"],
                     "Previous audit fingerprint mismatch")
            previous_summary = _json(previous_summary_bytes, "previous audit")
            _require(isinstance(previous_summary, dict) and previous_summary.get("audit_valid") is True and previous_summary.get("readiness") in {"ready", "partial", "zero_items"}
                     and previous_summary.get("blockers") == [] and previous_summary.get("date") == metadata["date"]
                     and previous_summary.get("schema_version") == "1.4" and isinstance(previous_summary.get("inputs"), dict),
                     "Previous audit was not an accepted page audit")
            previous_metadata_bytes = read("previous/metadata.json")
            _require(hashlib.sha256(previous_metadata_bytes).hexdigest() == previous_summary.get("evidence_metadata_sha256"),
                     "Previous metadata fingerprint missing or mismatched")
            previous_metadata = _json(previous_metadata_bytes, "previous metadata")
            try:
                validate("research-audit", previous_metadata)
            except BriefError as exc:
                raise BriefError("Invalid previous research-audit metadata schema") from exc
            _require(previous_metadata.get("page_scope") == previous_summary.get("page_scope")
                     and previous_metadata.get("page_scope", {}).get("contract") == "verified_pages_v1",
                     "Previous page audit scope mismatch")
            previous_report_bytes = read("previous/report.json")
            _require(hashlib.sha256(previous_report_bytes).hexdigest() == previous_summary.get("inputs", {}).get("draft_sha256")
                     == previous_metadata["inputs"]["draft_sha256"], "Previous report fingerprint mismatch")
            previous_report = _json(previous_report_bytes, "previous report")
            _require(isinstance(previous_report, dict) and previous_report.get("schema_version") == "1.2" and previous_report.get("date") == metadata["date"]
                     and type(previous_report.get("revision")) is int and previous_report["revision"] >= 1,
                     "Previous report identity mismatch")
            _require(previous_report.get("page_delivery") == previous_metadata["page_scope"]["page_delivery"],
                     "Previous report delivery differs from its audit")
            if "research_previous" in evidence:
                research = evidence["research_previous"]
                _require(all(research[key] == evidence[key] for key in ("audit_sha256", "archive_sha256")), "Correction research evidence differs")
                _require(research["request_id"] == "morning-" + metadata["date"].replace("-", "") + "-r" + str(previous_report["revision"]), "Correction research revision differs")
                chain = _json(read("previous/corrections.json", MAX_PACKAGE_BYTES), "correction chain")
                _require(isinstance(chain, list) and 0 < len(chain) < 100, "Bounded correction chain required")
                cursor = research
                try:
                    for item in chain:
                        _require(isinstance(item, dict) and set(item) == {"bundle_base64", "report_commit"}, "Correction chain fields")
                        _require(isinstance(item["bundle_base64"], str) and isinstance(item["report_commit"], str) and len(item["report_commit"]) == 40 and all(c in "0123456789abcdef" for c in item["report_commit"]), "Invalid correction chain values")
                        raw = base64.b64decode(item["bundle_base64"], validate=True)
                        _require(base64.b64encode(raw).decode() == item["bundle_base64"], "Canonical correction bundle")
                        bundle = _json(raw, "correction bundle")
                        _require(isinstance(bundle, dict) and isinstance(bundle.get("files"), list) and all(isinstance(f, dict) and isinstance(f.get("path"), str) for f in bundle["files"]), "Invalid correction bundle structure")
                        files = [f for f in bundle["files"] if f["path"].endswith("/report.json")]
                        _require(len(files) == 1, "Correction report missing")
                        report_bytes = base64.b64decode(files[0]["base64"], validate=True)
                        _require(hashlib.sha256(report_bytes).hexdigest() == files[0]["sha256"], "Correction report hash")
                        corrected = _json(report_bytes, "correction report")
                        rid = "morning-" + metadata["date"].replace("-", "") + "-r" + str(previous_report["revision"]+1)
                        _require(bundle["request_id"] == rid and isinstance(bundle.get("expected_base_sha"), str) and len(bundle["expected_base_sha"]) == 40 and all(c in "0123456789abcdef" for c in bundle["expected_base_sha"])
                                 and corrected["date"] == metadata["date"] and corrected["revision"] == previous_report["revision"]+1
                                 and corrected.get("schema_version") == "1.2"
                                 and corrected.get("revision_notice", {}).get("kind") in {"correction", "maintenance", "material_update"}, "Correction identity differs")
                        pages = corrected["page_delivery"]
                        _require(pages["previous"] == {key: cursor[key] for key in ("request_id", "bundle_sha256")}
                                 and pages["added"] == [] and pages["pending"] == previous_report["page_delivery"]["pending"]
                                 and [a["id"] for a in corrected["articles"]] == [a["id"] for a in previous_report["articles"]], "Correction cannot change page partition")
                        cursor = dict(request_id=rid,bundle_sha256=hashlib.sha256(raw).hexdigest(),report_commit=item["report_commit"])
                        previous_report = corrected
                except (BriefError, KeyError, TypeError, ValueError, AttributeError, binascii.Error) as exc:
                    raise BriefError("Invalid correction chain evidence") from exc
                _require(cursor == {key:evidence[key] for key in cursor}, "Correction latest publication fingerprint differs")
            current_revision = previous_report["revision"] + 1
            _require(previous["request_id"] == "morning-" + metadata["date"].replace("-", "") + "-r" + str(current_revision - 1),
                     "Previous request revision mismatch")
            old_articles = previous_report.get("articles")
            _require(isinstance(old_articles, list) and old_articles
                     and all(isinstance(article, dict) and article.get("id") in PAGE_CHECKS for article in old_articles),
                     "Invalid previous page articles")
            old_pages = [article["id"] for article in old_articles]
            _require(old_pages == [page for page in PAGE_CHECKS if page in old_pages]
                     and isinstance(previous_report.get("entries"), list)
                     and all(isinstance(entry, dict) and entry.get("article_id") in old_pages
                             and isinstance(entry.get("event_key"), str) for entry in previous_report["entries"]),
                     "Invalid previous page entries or order")
            old_pending = {item["id"] for item in previous_report["page_delivery"]["pending"]}
            _require(set(added_pages) <= old_pending and not set(added_pages) & set(old_pages),
                     "Successor can only add previously pending pages")
            _require(metadata["limits"] == previous_metadata["limits"] and metadata["started_at"] == previous_metadata["started_at"]
                     and metadata["budget_policy"] == previous_metadata["budget_policy"], "Successor cannot reset original budget")
            for key in ("prompt_commit", "prompt_hashes", "history_commit", "history_sha256"):
                _require(metadata["inputs"][key] == previous_metadata["inputs"][key], "Successor must retain original research inputs")
            _require(_time(metadata["finished_at"]) >= _time(previous_metadata["finished_at"]),
                     "Successor cannot precede its previous completed audit")
            _require(metadata.get("coverage_contract") == previous_metadata.get("coverage_contract"),
                     "Successor must preserve the original coverage contract")
            _require(community.successor_sources_preserved(metadata, previous_metadata),
                     "Successor must preserve all prior source records")
            if discovery_contract:
                prior_queries = previous_metadata["discovery"]["queries"]
                _require(metadata["discovery"]["queries"][:len(prior_queries)] == prior_queries,
                         "Successor must preserve all prior discovery records")
            for page in old_pages:
                for name in page_checks[page]:
                    _require(metadata["checks"][name] == previous_metadata["checks"][name],
                             "Previously verified page checks must remain unchanged")
            previous_calls = {call["stage"]: call for call in previous_metadata["model_calls"]}
            _require(set(previous_calls) == {"research", "editor"}, "Previous model stages missing")
            for call in metadata["model_calls"]:
                old = previous_calls[call["stage"]]
                attempts = call["attempt_records"]
                count = len(old["attempt_records"])
                _require(attempts[:count] == old["attempt_records"] and len(attempts) > count
                         and call["model"] == old["model"], "Successor must preserve model attempts and append a real page round")
                for attempt in attempts[count:]:
                    round_binding = attempt.get("page_round", {})
                    attempted_pages = round_binding.get("page_ids", [])
                    _require(round_binding.get("revision") == current_revision
                             and round_binding.get("previous_audit_sha256") == evidence["audit_sha256"]
                             and bool(attempted_pages) and set(attempted_pages) <= old_pending
                             and attempted_pages == [page for page in PAGE_CHECKS if page in attempted_pages],
                             "New model attempts must bind the exact current page round and canonical pending attempt set")
                successful = [attempt for attempt in attempts[count:] if attempt["status"] in {"ok", "partial"}]
                _require(not successful or set(added_pages) <= set(successful[-1]["page_round"]["page_ids"]),
                         "Last successful attempt must cover added pages in the exact current page round")
        ready_pages = [page for page in PAGE_CHECKS if page in old_pages + added_pages]
        pending_pages = [item["id"] for item in delivery["pending"]]
        _require(added_pages == page_scope["checked_pages"]
                 and added_pages == [page for page in PAGE_CHECKS if page in added_pages]
                 and ready_pages == [page for page in PAGE_CHECKS if page in ready_pages]
                 and pending_pages == [page for page in PAGE_CHECKS if page in pending_pages]
                 and len(set(ready_pages + pending_pages)) == len(ready_pages + pending_pages) == len(PAGE_CHECKS)
                 and set(ready_pages + pending_pages) == set(PAGE_CHECKS),
                 "Page scope must partition all six pages in canonical order")
        for item in delivery["pending"]:
            reason = item["reason"]
            _require(reason.strip() == reason and bool(reason)
                     and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in "<>\u2028\u2029\ufeff"
                                 or 0xd800 <= ord(c) <= 0xdfff for c in reason),
                     "Pending page reason contains forbidden characters or whitespace")
            _require(len(reason.encode("utf-8")) <= 300, "Pending page reason exceeds UTF-8 byte limit")
        pending_checks = {name for page in pending_pages for name in page_checks[page]}
        _require(all(metadata["checks"][name]["status"] in {"checked", "no_qualifying_items"}
                     for page in ready_pages for name in page_checks[page]),
                 "Ready pages require every assigned check to be completed")
    layered_budget = metadata["schema_version"] == "1.4"
    complete_contract = metadata["schema_version"] in {"1.3", "1.4"}
    for name, expected in metadata["inputs"]["prompt_hashes"].items():
        _require(hashlib.sha256(read(f"prompts/{name}")).hexdigest() == expected, "Prompt fingerprint mismatch")
    audit_prompt = read("prompts/audit.md")
    whole_edition = b"Group-Delivery: complete_six_topics_v1" in audit_prompt.splitlines()
    require_complete_first = whole_edition or metadata["date"] >= "2026-11-19"
    if (whole_edition and layered_budget and page_scope is None
            and b"Evidence-Package: whole_edition_32mb_v1" in audit_prompt.splitlines()):
        package_limit = WHOLE_EDITION_PACKAGE_BYTES
    new_prompt = community.PROMPT_MARKER.encode() in audit_prompt
    _require(discovery_contract == new_prompt, "Coverage contract must match the original frozen audit prompt")
    history_bytes = read("history.json")
    _require(hashlib.sha256(history_bytes).hexdigest() == metadata["inputs"]["history_sha256"], "History fingerprint mismatch")
    history = _json(history_bytes, "history")
    _require(isinstance(history, dict) and isinstance(history.get("entries"), list), "History must contain index entries")
    _require(isinstance(history.get("reports"), list), "History must contain report records")
    for report in history["reports"]:
        _require(isinstance(report, dict) and isinstance(report.get("date"), str), "Invalid history report record")
        try:
            valid_date = date.fromisoformat(report["date"]).isoformat() == report["date"]
        except ValueError:
            valid_date = False
        _require(valid_date, "Invalid history report date")
    _require(previous_report is not None or not any(report["date"] == metadata["date"] for report in history["reports"]),
             "Same-day corrections are outside the new-daily audit scope")
    history_entries = {}
    for entry in history["entries"]:
        _require(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip(), "Invalid history entry")
        _require(isinstance(entry.get("event_key"), str) and entry["event_key"].strip(), "Invalid history event key")
        _require(entry["id"] not in history_entries, "Duplicate history entry ID")
        history_entries[entry["id"]] = entry
    history_ids = set(history_entries)
    history_events = {entry["event_key"] for entry in history_entries.values()}
    candidates_bytes = read("candidates.json")
    _require(hashlib.sha256(candidates_bytes).hexdigest() == metadata["inputs"]["candidates_sha256"],
             "Candidates fingerprint mismatch")
    candidates = _json(candidates_bytes, "candidates")
    try:
        validate("candidates", candidates)
    except BriefError as exc:
        raise BriefError("Invalid candidates schema") from exc
    _require(candidates["mode"] == "live", "Audit requires actual live candidates, not mock fixtures")

    started = _time(metadata["started_at"])
    finished = _time(metadata["finished_at"])
    as_of = _time(metadata["as_of"])
    _require(started <= finished and as_of <= finished, "Invalid audit time ordering")
    _require(as_of.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() == metadata["date"],
             "Audit date must match as_of in Asia/Shanghai")
    limits = metadata["limits"]
    blockers = []
    warnings = []
    run_elapsed = (finished - started).total_seconds()
    if not layered_budget and run_elapsed > limits["run_seconds"]:
        blockers.append("run_budget_exceeded")

    accounting = {"successful_attempts": {"source": 0, "model": 0},
                  "successful_seconds": 0, "failed_attempts": {"source": 0, "model": 0},
                  "failed_seconds": 0, "unknown_attempts": {"source": 0, "model": 0},
                  "reserved_unknown_seconds": 0, "charged_seconds": 0}

    cancelled_operations = set()
    cancelled_observations = {}

    def timestamp_not_before(later, earlier, allow_precision=True):
        """仅拒绝记录精度区间已确定反向的顺序，不虚构更细时间。"""
        def lower(value):
            parsed = _time(value)
            match = re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.([0-9]+))?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value)
            _require(match is not None, "Unsupported observation timestamp precision")
            digits = match.group(1) if match and match.group(1) else ""
            seconds = Decimal(int(parsed.replace(microsecond=0).timestamp()))
            return seconds + Decimal("0." + (digits or "0")), Decimal(1).scaleb(-len(digits))
        _require(isinstance(later, str) and isinstance(earlier, str), "Invalid audit timestamp")
        with localcontext() as context:
            context.prec = max(len(later), len(earlier)) + 20
            late, precision = lower(later)
            early, _ = lower(earlier)
            return late + precision > early if allow_precision else late >= early


    def retained_not_dispatched(attempt, record):
        operation_id = attempt["operation_id"]
        _require(operation_id not in cancelled_operations, "Duplicate not-dispatched operation")
        cancelled_operations.add(operation_id)
        def artifact(sha):
            data = read(f"stage_artifacts/{sha}.json", MAX_PACKAGE_BYTES)
            _require(hashlib.sha256(data).hexdigest() == sha, "Not-dispatched evidence fingerprint mismatch")
            return _json(data, "not-dispatched evidence")
        result = artifact(attempt["not_dispatched_evidence_sha256"])
        expected_request = "morning-" + metadata["date"].replace("-", "") + "-r1"
        try:
            expected_input = source_identity(record["url"])["hash"]
            artifact_input = source_identity(result.get("url"))["hash"] if isinstance(result, dict) else None
        except ValueError as exc:
            raise BriefError("Unsupported source URL identity") from exc
        _require(isinstance(result, dict) and set(result) - {"grant_receipt_sha256"} == {"schema_version", "request_id", "operation_id", "url",
                     "input_sha256", "status", "dispatch_started", "source_requests", "elapsed_seconds", "reserved_seconds",
                     "observation_sha256", "grant_command_id", "grant_receipt_ts"}
                 and result.get("schema_version") == "source_not_dispatched_v1"
                 and result.get("request_id") == expected_request and result.get("operation_id") == operation_id
                 and artifact_input == expected_input and result.get("input_sha256") == expected_input
                 and result.get("status") == "not_dispatched" and result.get("dispatch_started") is False
                 and type(result.get("source_requests")) is int and result["source_requests"] == 0
                 and result.get("elapsed_seconds") is None and type(result.get("reserved_seconds")) in (int, float)
                 and result.get("reserved_seconds") == attempt["reserved_seconds"],
                 "Not-dispatched evidence binding mismatch")
        sha = result.get("observation_sha256")
        _require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha), "Not-dispatched observation fingerprint required")
        observation = artifact(sha)
        _require(isinstance(observation, dict)
                 and observation.get("artifact_kind") == "local_dispatch_observation_not_settlement"
                 and observation.get("status") == "not_dispatched_grant_expired"
                 and observation.get("request_id") == expected_request
                 and isinstance(observation.get("operation_ids"), list) and operation_id in observation["operation_ids"]
                 and observation.get("dispatch_started") is False
                 and type(observation.get("source_requests")) is int and observation["source_requests"] == 0
                 and observation.get("command_id") == result.get("grant_command_id")
                 and observation.get("receipt_ts") == result.get("grant_receipt_ts"),
                 "Not-dispatched observation binding mismatch")
        # 观察可覆盖比本次证据选择更大的批次，不强制补入无关来源。
        # 全轮预算完整性由许可时的宿主机权威账本独立核对。
        ids = observation["operation_ids"]
        _require(all(isinstance(op, str) for op in ids) and len(ids) == len(set(ids)),
                 "Not-dispatched observation operation IDs must be unique")
        receipt = result.get("grant_receipt_ts")
        _require(isinstance(receipt, str) and re.fullmatch(r"[0-9]{10}\.[0-9]{6}", receipt)
                 and isinstance(result.get("grant_command_id"), str)
                 and re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}", result["grant_command_id"]),
                 "Not-dispatched grant identity required")
        deadline = float(receipt) + 60
        if "grant_receipt_sha256" in result:
            grant_sha = result["grant_receipt_sha256"]
            _require(isinstance(grant_sha, str) and re.fullmatch(r"[a-f0-9]{64}", grant_sha),
                     "Not-dispatched grant receipt fingerprint required")
            raw = read(f"stage_artifacts/{grant_sha}.json", MAX_PACKAGE_BYTES)
            prefix = b"!ai-research control-receipt "
            _require(hashlib.sha256(raw).hexdigest() == grant_sha and raw.startswith(prefix),
                     "Not-dispatched grant receipt fingerprint or prefix mismatch")
            grant = _json(raw[len(prefix):], "original grant receipt")
            _require(isinstance(grant, dict) and grant.get("request_id") == expected_request
                     and grant.get("command_id") == result["grant_command_id"]
                     and grant.get("dispatch_granted") is True
                     and not grant.get("replayed") and not grant.get("grant_expired")
                     and isinstance(grant.get("operations"), list)
                     and any(isinstance(op, dict) and op.get("operation_id") == operation_id
                             and op.get("input_sha256") == expected_input for op in grant["operations"])
                     and isinstance(grant.get("run"), dict), "Not-dispatched grant receipt binding mismatch")
            lease = grant["run"].get("lease_until")
            _require(type(lease) in (int, float) and 0 < lease < 10**16,
                     "Not-dispatched original grant lease required")
            deadline = min(deadline, lease / 1000)
        timestamp_not_before(observation.get("observed_at"), observation.get("observed_at"))
        observed = _time(observation.get("observed_at"))
        _require(timestamp_not_before(observation["observed_at"], metadata["started_at"], False)
                 and timestamp_not_before(metadata["finished_at"], observation["observed_at"], False)
                 and observed.timestamp() >= max(deadline, float(receipt)),
                 "Not-dispatched observation must follow grant expiry")
        # 此处只校验留存字节，不代表宿主机授权。
        # 原始发布服务会独立将许可绑定至权威账本。
        cancelled_observations[operation_id] = observation["observed_at"]
        return observation["observed_at"]

    def source_observation_order(record, prior=None):
        _require(record["retrieved_at"] is not None or community.only_undispatched_attempts(record),
                 "Executed source requires a retrieval timestamp")
        _require(not community.only_undispatched_attempts(record) or record["retrieved_at"] is None,
                 "Undispatched source cannot claim a retrieval timestamp")
        attempts = record["attempt_records"]
        last_observation = None
        for number, attempt in enumerate(attempts):
            if attempt["status"] != "not_dispatched":
                continue
            observed = cancelled_observations[attempt["operation_id"]]
            if prior is not None and number >= len(prior["attempt_records"]) and not community.only_undispatched_attempts(prior):
                _require(prior["retrieved_at"] is not None and timestamp_not_before(observed, prior["retrieved_at"]),
                         "Appended cancellation must follow the original retained read")
            if last_observation is not None:
                _require(timestamp_not_before(observed, last_observation), "No-dispatch observations must preserve attempt order")
            last_observation = observed
            if any(a["status"] != "not_dispatched" for a in attempts[number+1:]):
                _require(record["retrieved_at"] is not None and timestamp_not_before(record["retrieved_at"], observed),
                         "Source retry retrieval must follow its no-dispatch observation")
            elif any(a["status"] != "not_dispatched" for a in attempts[:number]):
                _require(record["retrieved_at"] is not None and timestamp_not_before(observed, record["retrieved_at"]),
                         "Trailing no-dispatch observation must follow the retained read")

    continuation_source_bindings = ({}, {})

    def operation_budget(record, kind, index):
        # 缺计时不等于零耗时，也不豁免真实尝试次数或已知的超限。
        if not layered_budget and record["attempts"] > limits[f"{kind}_attempts"]:
            blockers.append(f"{kind}_budget_exceeded")
        elapsed = record["elapsed_seconds"]
        label = "Source" if kind == "source" else "Model"
        if elapsed is None:
            collection = "sources" if kind == "source" else "model_calls"
            warnings.append({"code": "timing_unavailable", "path": f"{collection}[{index}].elapsed_seconds"})
        else:
            _require(elapsed <= run_elapsed + (LAYERED_TIME_TOLERANCE if layered_budget else 0), f"{label} duration exceeds recorded run interval")
            if not layered_budget and elapsed > limits["operation_seconds"]:
                blockers.append(f"{kind}_budget_exceeded")
        _require(layered_budget or not any("prepublication_continuation_sha256" in a for a in record.get("attempt_records", [])),
                 "Model continuation requires the original layered budget")
        if layered_budget:
            attempts = record["attempt_records"]
            _require(len(attempts) == record["attempts"], "Attempt count does not match retained records")
            _require(attempts[-1]["status"] == record["status"], "Final attempt status mismatch")
            known = 0
            charged_attempts = {}
            charged_seconds_by_round = {}
            cap = limits["source_attempt_seconds" if kind == "source" else "model_stage_seconds"]
            for number, attempt in enumerate(attempts):
                duration = attempt["elapsed_seconds"]
                has_continuation = "prepublication_continuation_sha256" in attempt
                _require(not has_continuation or (kind == "model" and number > 0
                         and "page_round" not in attempt), "Model continuation requires a preceding attempt before first publication")
                not_dispatched = attempt["status"] == "not_dispatched"
                if not_dispatched:
                    _require(kind == "source" and duration is None, "Only sources may retain an undispatched reservation")
                    observed = retained_not_dispatched(attempt, record)
                    warnings.append({"code": "not_dispatched_reservation_retained", "path": f"sources[{index}].attempt_records[{number}]"})
                _require(attempt["reserved_seconds"] <= cap, "Attempt reservation exceeds original kind limit")
                _require(not attempt["error"] if attempt["status"] == "ok" else bool(attempt["error"].strip()),
                         "Attempt outcome needs a consistent error")
                if duration is None:
                    collection = "sources" if kind == "source" else "model_calls"
                    warnings.append({"code": "timing_unavailable", "path": f"{collection}[{index}].attempt_records[{number}].elapsed_seconds"})
                else:
                    known += duration
                    _require(duration <= run_elapsed + LAYERED_TIME_TOLERANCE, "Attempt duration exceeds recorded run interval")
                round_id = attempt.get("page_round", {}).get("revision", 1)
                if "page_round" in attempt:
                    _require(kind == "model" and previous_metadata is not None, "Page rounds require authenticated previous audit evidence")
                successful = attempt["status"] in {"ok", "partial"}
                if attempt["status"] == "unknown" or duration is None:
                    accounting["unknown_attempts"][kind] += 1
                    accounting["reserved_unknown_seconds"] += max(attempt["reserved_seconds"], duration or 0)
                    charged_attempts[round_id] = charged_attempts.get(round_id, 0) + 1
                    if attempt["status"] == "unknown" or (has_continuation and duration is None):
                        blockers.append("operation_outcome_unknown")
                elif successful:
                    accounting["successful_attempts"][kind] += 1
                    accounting["successful_seconds"] += duration
                    charged_attempts[round_id] = charged_attempts.get(round_id, 0) + 1
                    if duration > cap + LAYERED_TIME_TOLERANCE:
                        blockers.append(f"{kind}_budget_exceeded")
                else:
                    accounting["failed_attempts"][kind] += 1
                    accounting["failed_seconds"] += duration
                if successful or duration is None or attempt["status"] == "unknown":
                    charged_seconds_by_round[round_id] = charged_seconds_by_round.get(round_id, 0) + (
                        max(attempt["reserved_seconds"], duration or 0) if duration is None or attempt["status"] == "unknown" else duration)
                if number:
                    previous = attempts[number - 1]
                    continuation = has_continuation and validate_continuation(
                        metadata, record, attempt, previous, read, _require, _json, continuation_source_bindings)
                    preceding_round = previous.get("page_round", {}).get("revision", 1)
                    new_round = kind == "model" and previous_metadata is not None and round_id > preceding_round
                    known_source_read = kind == "source" and previous["status"] == "ok"
                    _require(continuation or known_source_read or previous["status"] in {"failed", "timeout"} or
                             (kind == "source" and previous["status"] == "not_dispatched") or
                             (new_round and previous["status"] in {"ok", "partial"}),
                             "Cannot retry an already completed or unknown attempt")
                    _require(round_id == preceding_round or (new_round and round_id <= current_revision),
                             "Page rounds must advance within authenticated publication revisions")
                    _require(previous["elapsed_seconds"] is not None or (kind == "source" and previous["status"] == "not_dispatched"), "Unknown attempt cannot be replayed")
                if kind == "model":
                    _require(not successful or attempt["result_sha256"] is not None, "Successful model attempt requires its saved result")
                    for field in ("input_sha256", "checkpoint_sha256", "result_sha256"):
                        value = attempt[field]
                        if value is not None:
                            data = read(f"stage_artifacts/{value}.json", MAX_PACKAGE_BYTES)
                            _require(hashlib.sha256(data).hexdigest() == value, "Model artifact fingerprint mismatch")
                    if number:
                        _require(previous["elapsed_seconds"] is not None, "Unknown model attempt cannot be replayed")
                        _require(previous["result_sha256"] is not None
                                 and attempt["checkpoint_sha256"] == previous["result_sha256"],
                                 "Model recovery must retain the preceding result checkpoint")
            _require(known <= run_elapsed + LAYERED_TIME_TOLERANCE, "Known sequential attempt durations exceed the run interval")
            if elapsed is not None:
                _require(all(a["elapsed_seconds"] is not None for a in attempts),
                         "Known aggregate duration requires complete per-attempt timing; retain and reconcile the known timing evidence")
                _require(elapsed + LAYERED_TIME_TOLERANCE >= known, "Cumulative duration is less than retained attempts")
                if kind == "model":
                    _require(abs(elapsed - known) <= LAYERED_TIME_TOLERANCE, "Model cumulative duration must equal retained attempt durations")
            if kind == "model" and any(seconds > cap + LAYERED_TIME_TOLERANCE for seconds in charged_seconds_by_round.values()):
                blockers.append("model_budget_exceeded")
            if any(count > limits[f"{kind}_attempts"] for count in charged_attempts.values()):
                blockers.append(f"{kind}_budget_exceeded")

    sources, source_text = {}, {}
    for index, source in enumerate(metadata["sources"]):
        identity = source["id"]
        _require(identity not in sources, "Duplicate source ID")
        sources[identity] = source
        _url(source["url"])
        unread = community.only_undispatched_attempts(source)
        _require(source["retrieved_at"] is not None or unread, "Executed source requires a retrieval timestamp")
        retrieved = _time(source["retrieved_at"]) if source["retrieved_at"] is not None else None
        if retrieved is not None:
            _require(started <= retrieved <= finished, "Source read time is outside this run")
            if source["published_at"] is not None:
                _require(_time(source["published_at"]) <= retrieved, "Source publication follows retrieval")
        _require(not unread or source["retrieved_at"] is None, "Undispatched source cannot claim a retrieval timestamp")
        operation_budget(source, "source", index)
        if layered_budget:
            source_observation_order(source)
        if previous_metadata is not None:
            old = next((item for item in previous_metadata["sources"] if item["id"] == identity), None)
            if old is not None and any(a["status"] == "not_dispatched" for a in source["attempt_records"]) and any(
                    a["status"] != "not_dispatched" for a in source["attempt_records"][len(old["attempt_records"]):]):
                _require(timestamp_not_before(source["retrieved_at"], previous_metadata["finished_at"]),
                         "Resumed source read must follow the previous audit finish")
        if community.only_undispatched_attempts(source):
            _require(source["content_sha256"] is None, "Undispatched source cannot retain response bytes")
        if community.executed_source_status(source) == "ok":
            _require(source["content_sha256"] is not None and not community.executed_source_outcome(source)[1]
                     and (not source["error"] if source["status"] == "ok" else bool(source["error"].strip())),
                     "Successful source needs content fingerprint and no error")
        else:
            _require((complete_contract or source["content_sha256"] is None) and bool(source["error"].strip()),
                     "Failed source needs an error, not a successful content fingerprint")
        if source["content_sha256"] is not None:
            # 原始来源可能超过元数据的单文件上限；总包预算仍约束完整原字节。
            content = read(f"evidence/{identity}.txt", MAX_PACKAGE_BYTES)
            _require(hashlib.sha256(content).hexdigest() == source["content_sha256"], "Source content fingerprint mismatch")
            try:
                source_text[identity] = content.decode("utf-8")
            except UnicodeError as exc:
                raise BriefError("Evidence must be the actual UTF-8 text read from the source") from exc
            if community.executed_source_status(source) == "ok":
                _require(bool(source_text[identity].strip()), "Successful source evidence is empty")

    if previous_metadata is not None:
        for prior in previous_metadata["sources"]:
            if any(a["status"] == "not_dispatched" for a in sources[prior["id"]]["attempt_records"]):
                source_observation_order(prior)
                source_observation_order(sources[prior["id"]], prior)
                _require(all(timestamp_not_before(previous_metadata["finished_at"], cancelled_observations[a["operation_id"]], False)
                             for a in prior["attempt_records"] if a["status"] == "not_dispatched"),
                         "Previous audit cannot finish before its retained no-dispatch observations")

    if previous_metadata is not None:
        previous_sources = {s["id"]: s for s in previous_metadata["sources"]}
        for source in sources.values():
            retained_count = len(previous_sources.get(source["id"], {}).get("attempt_records", []))
            _require(all(timestamp_not_before(cancelled_observations[a["operation_id"]], previous_metadata["finished_at"], False)
                         for a in source["attempt_records"][retained_count:] if a["status"] == "not_dispatched"),
                     "Appended cancellation must follow the previous audit finish")

    def references(ids):
        _require(len(ids) == len(set(ids)) and all(i in sources for i in ids), "Source references must be unique and known")
        return [sources[i] for i in ids]

    allow_partial = metadata.get("coverage_policy") == "publish_verified_with_gaps"
    discovery_read = lambda path: read(path, MAX_PACKAGE_BYTES)
    discovery_refs = community.validate_discovery(metadata, sources, source_text, candidates, history, _require, _time, discovery_read, previous_metadata) if discovery_contract else {}
    coverage_gaps, unchecked, checked = [], [], []
    if complete_contract:
        _require(metadata.get("coverage_contract") == (community.CONTRACT if discovery_contract else CONTRACT) and set(metadata["checks"]) == set(required_checks),
                 "Daily coverage requires every research direction and official source group")
    for name, check in metadata["checks"].items():
        linked = references(check["source_ids"])
        matching = linked if name in discovery_refs else [s for s in linked if source_matches(name, s["url"])] if complete_contract else linked
        if check["status"] in {"checked", "no_qualifying_items"}:
            _require(any(community.executed_source_status(s) == "ok" and (not complete_contract or s["attempts"] > 0) for s in matching),
                     "Completed coverage needs a successful source read from its required source group")
            if complete_contract:
                _require("failure_kind" not in check, "Completed coverage cannot claim a source failure")
            checked.append(name)
        elif complete_contract and check["status"] == "not_checked":
            unchecked.append(name)
            if page_scope is not None and name in pending_checks:
                coverage_gaps.append(name)
                warnings.append({"code": "coverage_incomplete", "path": f"checks.{name}"})
            else:
                blockers.append("required_coverage_not_checked")
        else:
            if complete_contract:
                _require(check.get("failure_kind") in FAILURE_KINDS and matching and all(s["attempts"] > 0 for s in matching)
                         and any(not community.only_undispatched_attempts(s) for s in matching),
                         "Failed coverage requires an attempted matching source and explicit failure kind")
                if check["failure_kind"] in {"transport", "access_denied"}:
                    statuses = {"failed", "timeout"} if check["failure_kind"] == "transport" else {"failed"}
                    _require(any(community.executed_source_status(s) in statuses for s in matching), "Access failure needs a failed source record")
                elif check["failure_kind"] == "unusable_response":
                    _require(any(community.executed_source_status(s) == "failed" and s["content_sha256"] is not None for s in matching),
                             "Unusable response needs retained failed-source bytes")
                else:
                    _require(any(s["content_sha256"] is not None for s in matching), "Unusable or incomplete response needs retained source bytes")
            coverage_gaps.append(name)
            if allow_partial:
                warnings.append({"code": "coverage_incomplete", "path": f"checks.{name}"})
            else:
                blockers.append("required_coverage_incomplete")

    calls, retained_sources, retained_source_text, prior_checks, prior_discovery = {}, {}, {}, {}, {}
    resolved_topic_reads = set()
    for index, call in enumerate(metadata["model_calls"]):
        _require(call["stage"] not in calls, "Duplicate model stage")
        calls[call["stage"]] = call
        model = call["model"]
        if model["name"] is None or model["version"] is None:
            _require(bool(model["unavailable_reason"].strip()), "Unavailable model identity needs an honest explanation")
        operation_budget(call, "model", index)
        resolution = call.get("coverage_resolution")
        if resolution is not None:
            _require(layered_budget and call["stage"] == "research" and call["status"] == "partial"
                     and allow_partial and (require_complete_first or not coverage_gaps) and not unchecked,
                     "Coverage resolution requires a retained partial research stage and completed current checks")
            attempt = call["attempt_records"][-1]
            _require(attempt["result_sha256"] is not None, "Coverage resolution needs the original research result")
            original = _json(read(f"stage_artifacts/{attempt['result_sha256']}.json", MAX_PACKAGE_BYTES), "research result")
            _require(isinstance(original, dict) and original.get("stage") == "research"
                     and original.get("stage_status") == "partial"
                     and original.get("partial_reason") == "coverage_incomplete"
                     and original.get("error") == attempt["error"] == call["error"]
                     and original.get("input_sha256") == attempt["input_sha256"]
                     and original.get("elapsed_seconds") == attempt["elapsed_seconds"],
                     "Coverage resolution must preserve the original partial result and timing")
            output_sha = original.get("output_sha256")
            _require(isinstance(output_sha, str) and re.fullmatch(r"[a-f0-9]{64}", output_sha),
                     "Coverage resolution needs the original research output fingerprint")
            output_bytes = read(f"stage_artifacts/{output_sha}.json", MAX_PACKAGE_BYTES)
            _require(hashlib.sha256(output_bytes).hexdigest() == output_sha, "Research output fingerprint mismatch")
            output = _json(output_bytes, "research output")
            original_candidates = output.get("candidates") if isinstance(output, dict) else None
            _require(isinstance(original_candidates, dict) and original_candidates.get("mode") == "live"
                     and original_candidates.get("items") == candidates["items"],
                     "Coverage resolution cannot introduce new research candidates")
            prior_checks = output.get("checks")
            _require(isinstance(prior_checks, dict) and set(prior_checks) == set(metadata["checks"])
                     and all(isinstance(check, dict) and isinstance(check.get("status"), str) and check.get("status") in
                             {"checked", "no_qualifying_items", "failed"} for check in prior_checks.values()),
                     "Coverage resolution requires the original completed research coverage record")
            prior_gaps = [name for name, check in prior_checks.items() if check["status"] == "failed"]
            _require(bool(prior_gaps), "Coverage resolution needs a real original gap")
            prior_sources = output.get("sources")
            _require(isinstance(prior_sources, list) and prior_sources,
                     "Coverage resolution needs original source attempts")
            try:
                validate("research-audit", dict(metadata, sources=prior_sources))
            except BriefError as exc:
                raise BriefError("Invalid retained research source or coverage schema") from exc
            prior_by_id = {}
            for prior in prior_sources:
                _require(isinstance(prior, dict) and isinstance(prior.get("id"), str),
                         "Original source identity is invalid")
                _require(prior["id"] not in prior_by_id, "Original source identity is duplicated")
                prior_by_id[prior["id"]] = prior
                current = sources.get(prior["id"])
                records = prior.get("attempt_records") if isinstance(prior, dict) else None
                _require(current is not None and current["url"] == prior.get("url")
                         and isinstance(records, list) and records
                         and all(isinstance(record, dict) for record in records)
                         and prior.get("attempts") == len(records)
                         and prior.get("status") == records[-1].get("status")
                         and current["attempt_records"][:len(records)] == records,
                         "Coverage resolution must retain all original source attempts")
                _require(community.cancelled_suffix_preserves_response(current, prior),
                         "Undispatched suffix must preserve original source response metadata")
                _require(len(current["attempt_records"]) > len(records) or current == prior,
                         "Changed retained sources require an appended budgeted attempt")
                _require(prior["elapsed_seconds"] is None or current["elapsed_seconds"] is None or
                         current["elapsed_seconds"] + LAYERED_TIME_TOLERANCE >= prior["elapsed_seconds"],
                         "Coverage resolution must preserve recorded cumulative source time")
                source_observation_order(prior)
                source_observation_order(current, prior)
                _require(not community.only_undispatched_attempts(prior) or prior["retrieved_at"] is None,
                         "Undispatched source cannot claim a retrieval timestamp")
                prior_hash = prior.get("content_sha256")
                _require(not community.only_undispatched_attempts(prior) or prior_hash is None,
                         "Undispatched source cannot retain response bytes")
                _require(community.executed_source_status(prior) != "ok" or (prior_hash is not None and not community.executed_source_outcome(prior)[1]
                         and (not prior["error"] if prior["status"] == "ok" else bool(prior["error"].strip()))),
                         "Original successful source needs retained response bytes")
                if prior_hash is not None:
                    _require(isinstance(prior_hash, str) and re.fullmatch(r"[a-f0-9]{64}", prior_hash),
                             "Original source fingerprint is invalid")
                    relative = f"evidence/{prior['id']}.txt" if current["content_sha256"] == prior_hash else f"evidence/{prior_hash}.txt"
                    prior_bytes = read(relative, MAX_PACKAGE_BYTES)
                    _require(hashlib.sha256(prior_bytes).hexdigest() == prior_hash,
                             "Original source response fingerprint mismatch")
                    try:
                        retained_source_text[prior["id"]] = prior_bytes.decode("utf-8")
                    except UnicodeError as exc:
                        raise BriefError("Original source response must be UTF-8") from exc
                    _require(community.executed_source_status(prior) != "ok" or bool(retained_source_text[prior["id"]].strip()),
                             "Original successful source evidence is empty")
                retained_sources[prior["id"]] = prior
            if discovery_contract:
                prior_discovery = output.get("discovery")
                _require(isinstance(prior_discovery, dict), "Original discovery record required for coverage resolution")
                # 研究输出可用 detail 说明，正式审计则使用 reason。
                # 上方已验证不可变原件的哈希，这里只规范化保留记录的副本。
                prior_audit_checks = {}
                for name, check in prior_checks.items():
                    normalized = dict(check)
                    if "detail" in normalized:
                        _require("reason" not in normalized, "Ambiguous original coverage explanation")
                        normalized["reason"] = normalized.pop("detail")
                    prior_audit_checks[name] = normalized
                prior_metadata = dict(metadata, checks=prior_audit_checks, discovery=prior_discovery, sources=prior_sources)
                try:
                    validate("research-audit", prior_metadata)
                except BriefError as exc:
                    raise BriefError("Invalid original discovery schema") from exc
                community.validate_discovery(prior_metadata, prior_by_id, retained_source_text, candidates, history, _require, _time, discovery_read, previous_metadata)
                _require(metadata["discovery"]["queries"][:len(prior_discovery["queries"])] == prior_discovery["queries"],
                         "Coverage resolution must append discovery evidence without rewriting original queries")
            for name, check in prior_checks.items():
                ids = check.get("source_ids")
                _require(isinstance(ids, list) and ids and all(isinstance(i, str) and i in prior_by_id for i in ids),
                         "Original coverage requires retained source references")
                matching = [prior_by_id[i] for i in ids if (discovery_contract and name in community.CHECKS) or source_matches(name, prior_by_id[i]["url"])]
                if check["status"] != "failed":
                    _require(any(community.executed_source_status(s) == "ok" for s in matching), "Original completed coverage needs successful evidence")
                    continue
                kind = check.get("failure_kind")
                _require(isinstance(kind, str) and kind in FAILURE_KINDS and matching
                         and any(not community.only_undispatched_attempts(s) for s in matching),
                         "Original failed coverage needs an explicit failure kind and matching source")
                if kind in {"transport", "access_denied"}:
                    statuses = {"failed", "timeout"} if kind == "transport" else {"failed"}
                    _require(any(community.executed_source_status(s) in statuses for s in matching), "Original access failure needs failed-source evidence")
                elif kind == "unusable_response":
                    _require(any(community.executed_source_status(s) == "failed" and s.get("content_sha256") is not None for s in matching),
                             "Original unusable response needs retained failed-source bytes")
                else:
                    _require(any(s.get("content_sha256") is not None for s in matching),
                             "Original incomplete listing needs retained source bytes")
            resolved = references(resolution["source_ids"])
            _require(all(any(a["status"] == "ok" for a in source["attempt_records"][
                         len(prior_by_id[source["id"]]["attempt_records"]) if source["id"] in prior_by_id else 0:])
                         for source in resolved),
                     "Reused resolution sources require an appended budgeted attempt with a successful read")
            _require(isinstance(original.get("started_at"), str) and isinstance(original.get("finished_at"), str),
                     "Original research timestamps are required")
            research_started, research_finished = _time(original["started_at"]), _time(original["finished_at"])
            _require(started <= research_started <= research_finished <= finished
                     and (attempt["elapsed_seconds"] is None or
                          abs((research_finished - research_started).total_seconds() - attempt["elapsed_seconds"]) <= LAYERED_TIME_TOLERANCE),
                     "Original research timing must match its audited run and duration")
            _require(all((community.only_undispatched_attempts(prior) and prior["retrieved_at"] is None)
                         or (prior["retrieved_at"] is not None and started <= _time(prior["retrieved_at"]) <= research_finished
                         and (prior["published_at"] is None or
                              _time(prior["published_at"]) <= _time(prior["retrieved_at"]))) for prior in prior_sources),
                     "Original research cannot finish before its retained sources")
            _require(all(timestamp_not_before(original["finished_at"], cancelled_observations[a["operation_id"]], False)
                         for prior in prior_sources for a in prior["attempt_records"] if a["status"] == "not_dispatched"),
                     "Original research cannot finish before its retained no-dispatch observations")
            _require(all(community.executed_source_status(source) == "ok" and _time(source["retrieved_at"]) > research_finished for source in resolved),
                     "Coverage resolution needs successful evidence retrieved after the original result")
            supported = {name for name in prior_gaps if any(
                source["id"] in metadata["checks"][name]["source_ids"]
                and ((discovery_contract and name in community.CHECKS) or source_matches(name, source["url"]))
                for source in resolved)}
            repaired = set(prior_gaps) - set(coverage_gaps)
            _require(repaired and repaired <= supported,
                     "Coverage resolution must bind later evidence for every original gap marked complete")
            resolved_topic_reads = {(name, source["id"]) for name in repaired for source in resolved
                                    if source["id"] in metadata["checks"][name]["source_ids"]
                                    and ((discovery_contract and name in community.CHECKS) or source_matches(name, source["url"]))}
            code = "research_coverage_partially_resolved" if coverage_gaps else "research_coverage_resolved"
            warnings.append({"code": code, "path": f"model_calls[{index}]"})
        if call["status"] == "ok":
            _require(not call["error"], "Successful model stage cannot contain an error")
        elif call["status"] == "partial":
            _require(allow_partial and (coverage_gaps or resolution is not None),
                     "Partial research requires an explicit coverage policy and real gaps or verified resolution")
            warnings.append({"code": "research_partial", "path": f"model_calls[{index}]"})
        else:
            _require(bool(call["error"].strip()), "Failed model stage needs an error")
            blockers.append("model_stage_failed")
    if set(calls) != {"research", "editor"}:
        blockers.append("model_stage_missing")

    first_edition = previous_report is None and require_complete_first
    if first_edition:
        research = calls.get("research")
        handoff, snapshot = _settled_research(research, read)
        settled_checks, settled_discovery, bound = {}, {}, {}
        if not prior_checks and "checks" in snapshot:
            originals = snapshot.get("sources")
            checks, discovery = snapshot["checks"], snapshot.get("discovery", {})
            _require(isinstance(checks, dict) and isinstance(discovery, dict) and isinstance(originals, list)
                     and all(isinstance(item, dict) for item in checks.values()), "Invalid settled topic evidence")
            settled_checks = {key: dict(item) for key, item in checks.items()}
            for item in settled_checks.values():
                if "detail" in item:
                    _require("reason" not in item, "Ambiguous original coverage explanation")
                    item["reason"] = item.pop("detail")
            validation = dict(metadata, checks=settled_checks, sources=originals)
            if "discovery" in snapshot:
                validation["discovery"] = discovery
            validate("research-audit", validation)
            settled_discovery = discovery
            for original in originals:
                ref, fingerprint = original["id"], original["content_sha256"]
                current = sources.get(ref)
                if not (current and current["url"] == original["url"] and community.executed_source_status(original) == "ok"
                        and isinstance(fingerprint, str) and re.fullmatch(r"[a-f0-9]{64}", fingerprint)):
                    continue
                attempts = original.get("attempt_records")
                if attempts and current.get("attempt_records", [])[:len(attempts)] != attempts:
                    continue
                if fingerprint == current["content_sha256"]:
                    text = source_text[ref]
                else:
                    relative = f"evidence/{fingerprint}.txt"
                    if not (root / relative).is_file():
                        continue
                    raw = read(relative, MAX_PACKAGE_BYTES)
                    _require(hashlib.sha256(raw).hexdigest() == fingerprint, "Original source response fingerprint mismatch")
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeError as exc:
                        raise BriefError("Original source response must be UTF-8") from exc
                _require(bool(text.strip()), "Original successful source evidence is empty")
                bound[ref] = retained_sources[ref] = original
                retained_source_text[ref] = text

    validate_current_outputs(metadata, candidates, read, _require, _json)
    by_event = {}
    for candidate in candidates["items"]:
        key = candidate["event_key"]
        _require(key not in by_event, "Duplicate candidate event key")
        by_event[key] = candidate
    decisions, candidate_reads = {}, {}
    for decision in metadata["decisions"]:
        key = decision["event_key"]
        _require(key in by_event and key not in decisions, "Each decision must name one unique candidate")
        decisions[key] = decision
        _require(set(decision["history_entry_ids"]) <= history_ids, "History references must exist in the retained index")
        linked = references(decision["source_ids"])
        candidate = by_event[key]
        versions = [(s, source_text.get(s["id"], "")) for s in linked]
        versions.extend((retained_sources[s["id"]], retained_source_text.get(s["id"], ""))
                        for s in linked if s["id"] in retained_sources)
        candidate_versions = [(retained_sources[s["id"]], retained_source_text.get(s["id"], ""))
                              for s in linked if s["id"] in retained_sources] if prior_checks else versions
        matching = [(s, text) for s, text in candidate_versions if community.executed_source_status(s) == "ok" and s["url"] == candidate["source_url"]
                    and s["published_at"] is not None
                    and _time(s["published_at"]) == _time(candidate["published_at"])
                    and _time(s["retrieved_at"]) == _time(candidate["retrieved_at"])]
        _require(bool(matching), "Candidate needs matching publication, retrieval and source evidence")
        candidate_reads[key] = {(s["id"], s["content_sha256"]) for s, text in matching if candidate["evidence"] in text}
        _require(bool(candidate_reads[key]), "Candidate evidence must occur in the retained source text")
        if decision["disposition"] in {"duplicate", "update"}:
            _require(bool(decision["history_entry_ids"]), "Duplicate/update decisions require history entry references")
        if decision["disposition"] == "update":
            _require(all(history_entries[i]["event_key"] == key for i in decision["history_entry_ids"]),
                     "Update references must target the same historical event")
        if decision["disposition"] == "selected":
            _require(key not in history_events, "Previously reported event requires an audited update")
        if decision["disposition"] in SELECTED:
            _require(candidate["source_type"] == "primary", "Selected candidates require primary source evidence")
            age = (as_of - _time(candidate["published_at"])).total_seconds()
            _require(0 <= age <= metadata["window_hours"] * 3600, "Selected candidate is outside the research window")
            _require(all(any(version["id"] == s["id"] and community.executed_source_status(version) == "ok"
                             for version, _ in versions) for s in linked),
                     "Selected evidence contains a failed source")
    _require(set(decisions) == set(by_event), "Every candidate needs a selection or exclusion decision")

    selected = {key for key, decision in decisions.items() if decision["disposition"] in SELECTED}
    retained_keys = set()
    if previous_summary is not None:
        # 旧摘要没有累计事件身份，只认可元数据绑定的已选决定。
        # 不把无法追溯的历史计数当作证据。
        prior_keys = previous_summary.get("verified_event_keys")
        if prior_keys is None:
            prior_keys = [d["event_key"] for d in previous_metadata["decisions"] if d["disposition"] in SELECTED]
        _require(isinstance(prior_keys, list) and all(isinstance(k, str) for k in prior_keys)
                 and len(prior_keys) == len(set(prior_keys)), "Invalid previous verified event identities")
        retained_keys = set(prior_keys) & {entry["event_key"] for entry in previous_report["entries"]}
    verified_keys = retained_keys | selected
    selected_count = len(verified_keys)
    if allow_partial and coverage_gaps and not selected_count:
        blockers.append("partial_report_requires_verified_items")
    notice = metadata.get("coverage_notice")
    if allow_partial and coverage_gaps:
        _require(notice is not None and set(notice["checks"]) == set(coverage_gaps),
                 "Coverage notice must disclose every incomplete check exactly once")
        prefix = notice_prefix(coverage_gaps) if complete_contract else "部分晨报｜覆盖缺口：" + "、".join(COVERAGE_LABELS[name] for name in COVERAGE_LABELS if name in coverage_gaps) + "。"
        _require(notice["text"].startswith(prefix) and bool(notice["text"][len(prefix):].strip()),
                 "Coverage notice needs the explicit partial-report heading and gap details")
        try:
            notice_bytes = notice["text"].encode("utf-8")
        except UnicodeError as exc:
            raise BriefError("Coverage notice must be valid UTF-8") from exc
        _require(len(notice_bytes) <= 180 and not any(ord(c) < 32 or ord(c) == 127 or c in "<>" for c in notice["text"]),
                 "Coverage notice exceeds the public subtitle byte limit or contains forbidden characters")
    else:
        _require(notice is None, "Coverage notice is only valid for explicitly permitted incomplete coverage")
    status_entries = {}
    for entry in metadata["status_entries"]:
        key = entry["event_key"]
        _require(key not in by_event and key not in status_entries and key not in history_events,
                 "Coverage status must be distinct from candidates and historical events")
        linked = references(entry["source_ids"])
        _require(linked and all(community.executed_source_status(source) == "ok" for source in linked),
                 "Coverage status requires successful source evidence")
        status_entries[key] = entry
    topic_reads, discovered_reads = {}, {}
    if first_edition:
        check_tables = ((metadata["checks"], sources), (prior_checks, retained_sources))
        discovery_tables = ((metadata.get("discovery", {}), sources), (prior_discovery, retained_sources))
        if not prior_checks:
            check_tables = ((settled_checks, bound),)
            discovery_tables = ((settled_discovery, bound),)
        for page, checks in page_checks.items():
            topic_reads[page] = {(ref, records[ref]["content_sha256"])
                                for table, records in check_tables
                                for check in checks for ref in table.get(check, {}).get("source_ids", ())
                                if ref in records and community.executed_source_status(records[ref]) == "ok"
                                and (not prior_checks or table is prior_checks or (check, ref) in resolved_topic_reads)
                                and (check in community.CHECKS or source_matches(check, records[ref]["url"]))}
        for discovery, records in discovery_tables:
            for query in discovery.get("queries", []):
                for result in query["results"]:
                    if result["disposition"] in SELECTED:
                        discovered_reads.setdefault((query["family"], result["event_key"]), set()).update(
                            (ref, records[ref]["content_sha256"]) for ref in result["source_ids"]
                            if ref in records and community.executed_source_status(records[ref]) == "ok"
                            and (not prior_checks or discovery is prior_discovery or (query["family"], ref) in resolved_topic_reads))
    supported_entries = True
    draft_sha = metadata["inputs"]["draft_sha256"]
    if draft_sha is None:
        blockers.append("draft_missing")
    else:
        draft_bytes = read("draft.json")
        _require(hashlib.sha256(draft_bytes).hexdigest() == draft_sha, "Draft fingerprint mismatch")
        draft = _json(draft_bytes, "draft")
        _require(isinstance(draft, dict) and draft.get("date") == metadata["date"] and isinstance(draft.get("entries"), list),
                 "Draft must name this date and contain report entries")
        _require(type(draft.get("revision")) is int and draft["revision"] == current_revision,
                 "Audit supports new daily revision 1 only" if previous_report is None
                 else "Audit revision must follow its exact previous page evidence")
        if page_scope is not None:
            _require(draft.get("schema_version") == "1.2" and draft.get("page_delivery") == page_scope["page_delivery"],
                     "Draft page delivery must match the explicit page scope")
            articles = draft.get("articles")
            _require(isinstance(articles, list) and all(isinstance(article, dict) for article in articles)
                     and [article.get("id") for article in articles] == ready_pages,
                     "Draft articles must be exactly the newly verified pages")
            _require(all(isinstance(entry, dict) and entry.get("article_id") in ready_pages for entry in draft["entries"])
                     and {entry["article_id"] for entry in draft["entries"]} == set(ready_pages),
                     "Draft entries must belong to newly verified pages")
        if notice is not None:
            _require(draft.get("subtitle") == notice["text"], "Draft subtitle must preserve the complete coverage notice")
        old_entries = []
        if previous_report is not None:
            old_articles = [article for article in draft["articles"] if article["id"] in old_pages]
            _require(old_articles == previous_report["articles"], "Previously published articles must remain unchanged")
            old_entries = [entry for entry in draft["entries"] if entry["article_id"] in old_pages]
            _require(old_entries == previous_report["entries"], "Previously published entries must remain unchanged")
        old_keys = {entry["event_key"] for entry in old_entries}
        _require(not old_keys & (selected | set(status_entries)), "Current candidates cannot redo old page events")
        entries, seen_keys = {}, set()
        for entry in draft["entries"]:
            _require(isinstance(entry, dict) and isinstance(entry.get("event_key"), str), "Invalid draft entry")
            key = entry["event_key"]
            _require(key not in seen_keys, "Draft contains a duplicate event across pages")
            seen_keys.add(key)
            if key in old_keys:
                _require(entry in old_entries, "Only exact retained entries may bypass new evidence checks")
                continue
            _require(key in selected | status_entries.keys() and key not in entries, "Draft contains an excluded, unknown or duplicate event")
            entries[key] = entry
            urls = entry.get("source_urls")
            binding = status_entries[key] if key in status_entries else decisions[key]
            permitted = {sources[i]["url"] for i in binding["source_ids"]}
            _require(isinstance(urls, list) and urls and all(isinstance(url, str) and url in permitted for url in urls)
                     and (key in status_entries or by_event[key]["source_url"] in urls),
                     "Draft sources differ from retained candidate evidence")
            if first_edition:
                page = entry.get("article_id")
                if not isinstance(page, str) or page not in topic_reads:
                    supported_entries = False
                else:
                    status = key in status_entries
                    allowed = set(topic_reads[page])
                    for check in page_checks[page]:
                        allowed.update(discovered_reads.get((check, key), ()))
                    if status:
                        for item in handoff.get("page_status_sources", {}).get(page, []):
                            if not isinstance(item, dict) or item.get("source_id") not in binding["source_ids"]:
                                continue
                            ref = item["source_id"]
                            for records in (sources, retained_sources):
                                version = records.get(ref)
                                if (version and community.executed_source_status(version) == "ok"
                                        and (version["url"], version["content_sha256"]) == (item.get("url"), item.get("content_sha256"))):
                                    allowed.add((ref, version["content_sha256"]))
                        supported_entries &= set(urls) <= {sources[ref]["url"] for ref, _ in allowed if ref in binding["source_ids"]}
                    else:
                        assignments = [item for item in handoff.get("candidate_bindings_and_decisions", [])
                                       if isinstance(item, dict) and item.get("event_key") == key and item.get("article_id")]
                        if assignments:
                            supported_entries &= all(item["article_id"] == page and _assigned_primary_matches(
                                item, handoff, by_event[key]["source_url"], candidate_reads[key]) for item in assignments)
                        else:
                            supported_entries &= bool(candidate_reads[key] & allowed)
            if key in decisions and decisions[key]["disposition"] == "update":
                _require(entry.get("update_of") in decisions[key]["history_entry_ids"], "Draft update must preserve its history relationship")
            else:
                _require(entry.get("update_of") is None, "Draft cannot introduce an unaudited update relationship")
        _require(set(entries) == selected | status_entries.keys(), "Draft must contain every selected event and coverage status exactly once")

    if layered_budget:
        accounting["charged_seconds"] = accounting["successful_seconds"] + accounting["reserved_unknown_seconds"]
        if accounting["charged_seconds"] > limits["run_seconds"] + LAYERED_TIME_TOLERANCE:
            blockers.append("run_budget_exceeded")
    if first_edition:
        articles = draft.get("articles") if draft_sha is not None else None
        report_fields = {"schema_version", "date", "revision", "title", "subtitle", "articles", "entries", "images", "poster"}
        if (page_scope is not None or not isinstance(articles, list) or draft.get("schema_version") != "1.0"
                or type(draft.get("revision")) is not int or draft["revision"] != 1 or set(draft) != report_fields
                or not all(isinstance(a, dict) for a in articles)
                or [a.get("id") for a in articles] != list(PAGE_CHECKS)
                or not all(isinstance(entry.get("article_id"), str) and entry["article_id"] in PAGE_CHECKS for entry in draft["entries"])
                or {entry["article_id"] for entry in draft["entries"]} != set(PAGE_CHECKS) or not supported_entries):
            blockers.append("complete_edition_required")

    result = {"schema_version": metadata["schema_version"], "date": metadata["date"], "audit_valid": True,
            "readiness": "blocked" if blockers else ("partial" if coverage_gaps else ("ready" if selected_count else "zero_items")),
            "blockers": sorted(set(blockers)), "candidate_count": len(by_event), "selected_count": selected_count,
            "status_entry_count": len(status_entries), "delivery_allowed": False}
    if metadata["schema_version"] != "1.0":
        # 摘要只含固定字段路径，不回显私有解释、来源地址或正文。
        result.update(warnings=warnings, budget_unknown=any(w["code"] == "timing_unavailable" for w in warnings))
    if metadata["schema_version"] in {"1.2", "1.3", "1.4"}:
        result["coverage_incomplete"] = sorted(coverage_gaps)
    if complete_contract:
        result.update(coverage_contract=metadata["coverage_contract"], required_checks=list(required_checks),
                      coverage_checked=sorted(checked), coverage_unchecked=sorted(unchecked), limits=dict(limits),
                      inputs={key: metadata["inputs"][key] for key in
                              ("prompt_commit", "history_commit", "prompt_hashes", "history_sha256", "draft_sha256")})
    if page_scope is not None:
        result["page_scope"] = page_scope
        result["verified_event_keys"] = sorted(verified_keys)
    if layered_budget:
        result.update(evidence_metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest(),
                      budget_accounting=accounting, run_elapsed_seconds=run_elapsed,
                      budget_policy=metadata["budget_policy"])
        result["budget_unknown"] = result["budget_unknown"] or any(accounting["unknown_attempts"].values())
    return result
