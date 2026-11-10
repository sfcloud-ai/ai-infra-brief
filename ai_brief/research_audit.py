"""只读验证真实晨报的私有证据包，不联网、不发布、不修改投递状态。"""

from datetime import date
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .contracts import parse_time, validate
from .storage import BriefError


MAX_FILE_BYTES = 2_000_000
MAX_PACKAGE_BYTES = 20_000_000
SELECTED = {"selected", "update"}


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

    def read(relative, max_bytes=MAX_FILE_BYTES):
        nonlocal total
        path = root / relative
        _require(not path.is_symlink() and not path.parent.is_symlink(), "Audit files must not be symlinks")
        _require(path.is_file(), f"Missing audit file: {relative}")
        _require(path.stat().st_size <= max_bytes, "Audit file exceeds size limit")
        remaining = min(max_bytes, MAX_PACKAGE_BYTES - total)
        with path.open("rb") as stream:
            data = stream.read(remaining + 1)
        total += len(data)
        _require(len(data) <= max_bytes and total <= MAX_PACKAGE_BYTES, "Audit package exceeds size limit")
        return data

    metadata = _json(read("audit.json"), "audit")
    try:
        validate("research-audit", metadata)
    except BriefError as exc:
        # 不把完整候选、摘录或私人路径回显到运行日志。
        raise BriefError("Invalid research-audit metadata schema") from exc
    for name, expected in metadata["inputs"]["prompt_hashes"].items():
        _require(hashlib.sha256(read(f"prompts/{name}")).hexdigest() == expected, "Prompt fingerprint mismatch")
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
    _require(not any(report["date"] == metadata["date"] for report in history["reports"]),
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
    if run_elapsed > limits["run_seconds"]:
        blockers.append("run_budget_exceeded")

    def operation_budget(record, kind, index):
        # 缺计时不等于零耗时，也不豁免真实尝试次数或已知的超限。
        if record["attempts"] > limits[f"{kind}_attempts"]:
            blockers.append(f"{kind}_budget_exceeded")
        elapsed = record["elapsed_seconds"]
        label = "Source" if kind == "source" else "Model"
        if elapsed is None:
            collection = "sources" if kind == "source" else "model_calls"
            warnings.append({"code": "timing_unavailable", "path": f"{collection}[{index}].elapsed_seconds"})
        else:
            _require(elapsed <= run_elapsed, f"{label} duration exceeds recorded run interval")
            if elapsed > limits["operation_seconds"]:
                blockers.append(f"{kind}_budget_exceeded")

    sources, source_text = {}, {}
    for index, source in enumerate(metadata["sources"]):
        identity = source["id"]
        _require(identity not in sources, "Duplicate source ID")
        sources[identity] = source
        _url(source["url"])
        retrieved = _time(source["retrieved_at"])
        _require(started <= retrieved <= finished, "Source read time is outside this run")
        if source["published_at"] is not None:
            _require(_time(source["published_at"]) <= retrieved, "Source publication follows retrieval")
        operation_budget(source, "source", index)
        if source["status"] == "ok":
            _require(source["content_sha256"] is not None and not source["error"], "Successful source needs content fingerprint and no error")
            # 原始来源可能超过元数据的单文件上限；总包预算仍约束完整原字节。
            content = read(f"evidence/{identity}.txt", MAX_PACKAGE_BYTES)
            _require(hashlib.sha256(content).hexdigest() == source["content_sha256"], "Source content fingerprint mismatch")
            try:
                source_text[identity] = content.decode("utf-8")
            except UnicodeError as exc:
                raise BriefError("Evidence must be the actual UTF-8 text read from the source") from exc
            _require(bool(source_text[identity].strip()), "Successful source evidence is empty")
        else:
            _require(source["content_sha256"] is None and bool(source["error"].strip()), "Failed source needs an error, not a successful content fingerprint")

    def references(ids):
        _require(len(ids) == len(set(ids)) and all(i in sources for i in ids), "Source references must be unique and known")
        return [sources[i] for i in ids]

    for check in metadata["checks"].values():
        linked = references(check["source_ids"])
        if check["status"] in {"checked", "no_qualifying_items"}:
            _require(any(s["status"] == "ok" for s in linked), "Completed coverage needs a successful source read")
        else:
            blockers.append("required_coverage_incomplete")

    calls = {}
    for index, call in enumerate(metadata["model_calls"]):
        _require(call["stage"] not in calls, "Duplicate model stage")
        calls[call["stage"]] = call
        model = call["model"]
        if model["name"] is None or model["version"] is None:
            _require(bool(model["unavailable_reason"].strip()), "Unavailable model identity needs an honest explanation")
        operation_budget(call, "model", index)
        if call["status"] == "ok":
            _require(not call["error"], "Successful model stage cannot contain an error")
        else:
            _require(bool(call["error"].strip()), "Failed model stage needs an error")
            blockers.append("model_stage_failed")
    if set(calls) != {"research", "editor"}:
        blockers.append("model_stage_missing")

    by_event = {}
    for candidate in candidates["items"]:
        key = candidate["event_key"]
        _require(key not in by_event, "Duplicate candidate event key")
        by_event[key] = candidate
    decisions = {}
    for decision in metadata["decisions"]:
        key = decision["event_key"]
        _require(key in by_event and key not in decisions, "Each decision must name one unique candidate")
        decisions[key] = decision
        _require(set(decision["history_entry_ids"]) <= history_ids, "History references must exist in the retained index")
        linked = references(decision["source_ids"])
        candidate = by_event[key]
        matching = [s for s in linked if s["status"] == "ok" and s["url"] == candidate["source_url"]
                    and s["published_at"] is not None
                    and _time(s["published_at"]) == _time(candidate["published_at"])
                    and _time(s["retrieved_at"]) == _time(candidate["retrieved_at"])]
        _require(bool(matching), "Candidate needs matching publication, retrieval and source evidence")
        _require(any(candidate["evidence"] in source_text[s["id"]] for s in matching), "Candidate evidence must occur in the retained source text")
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
            _require(all(s["status"] == "ok" for s in linked), "Selected evidence contains a failed source")
    _require(set(decisions) == set(by_event), "Every candidate needs a selection or exclusion decision")

    selected = {key for key, decision in decisions.items() if decision["disposition"] in SELECTED}
    status_entries = {}
    for entry in metadata["status_entries"]:
        key = entry["event_key"]
        _require(key not in by_event and key not in status_entries and key not in history_events,
                 "Coverage status must be distinct from candidates and historical events")
        linked = references(entry["source_ids"])
        _require(linked and all(source["status"] == "ok" for source in linked),
                 "Coverage status requires successful source evidence")
        status_entries[key] = entry
    draft_sha = metadata["inputs"]["draft_sha256"]
    if draft_sha is None:
        blockers.append("draft_missing")
    else:
        draft_bytes = read("draft.json")
        _require(hashlib.sha256(draft_bytes).hexdigest() == draft_sha, "Draft fingerprint mismatch")
        draft = _json(draft_bytes, "draft")
        _require(isinstance(draft, dict) and draft.get("date") == metadata["date"] and isinstance(draft.get("entries"), list),
                 "Draft must name this date and contain report entries")
        _require(type(draft.get("revision")) is int and draft["revision"] == 1,
                 "Audit supports new daily revision 1 only")
        entries = {}
        for entry in draft["entries"]:
            _require(isinstance(entry, dict) and isinstance(entry.get("event_key"), str), "Invalid draft entry")
            key = entry["event_key"]
            _require(key in selected | status_entries.keys() and key not in entries, "Draft contains an excluded, unknown or duplicate event")
            entries[key] = entry
            urls = entry.get("source_urls")
            binding = status_entries[key] if key in status_entries else decisions[key]
            permitted = {sources[i]["url"] for i in binding["source_ids"]}
            _require(isinstance(urls, list) and urls and all(isinstance(url, str) and url in permitted for url in urls)
                     and (key in status_entries or by_event[key]["source_url"] in urls),
                     "Draft sources differ from retained candidate evidence")
            if key in decisions and decisions[key]["disposition"] == "update":
                _require(entry.get("update_of") in decisions[key]["history_entry_ids"], "Draft update must preserve its history relationship")
            else:
                _require(entry.get("update_of") is None, "Draft cannot introduce an unaudited update relationship")
        _require(set(entries) == selected | status_entries.keys(), "Draft must contain every selected event and coverage status exactly once")

    result = {"schema_version": metadata["schema_version"], "date": metadata["date"], "audit_valid": True,
            "readiness": "blocked" if blockers else ("ready" if selected else "zero_items"),
            "blockers": sorted(set(blockers)), "candidate_count": len(by_event), "selected_count": len(selected),
            "status_entry_count": len(status_entries), "delivery_allowed": False}
    if metadata["schema_version"] == "1.1":
        # 摘要只含固定字段路径，不回显私有解释、来源地址或正文。
        result.update(warnings=warnings, budget_unknown=bool(warnings))
    return result
