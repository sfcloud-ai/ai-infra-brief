"""审计跨仓发现的原始查询与候选去向；不联网，也不证明搜索穷尽。"""
import json
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit, unquote
from .coverage import REQUIRED_CHECKS, source_matches

CONTRACT = "daily_required_v2"
PROMPT_MARKER = "Coverage-Contract: daily_required_v2"
CHECKS = ("discovery_memory", "discovery_scheduling", "discovery_compute", "discovery_data_path")
PAGE_CHECKS = {
    "model-serving-gpu": ("discovery_memory", "discovery_scheduling", "discovery_compute"),
    "hardware-status": ("discovery_data_path",),
}


def only_undispatched_attempts(source):
    """区分保留许可与实际来源请求，供当前和历史证据共用。"""
    attempts = source.get("attempt_records", [])
    return bool(attempts) and all(a["status"] == "not_dispatched" for a in attempts)


def executed_source_outcome(source):
    """预留但未发起的重试不能抹去最近一次实际请求结果。"""
    for attempt in reversed(source.get("attempt_records", [])):
        if attempt["status"] != "not_dispatched":
            return attempt["status"], attempt["error"]
    return (None, source["error"]) if only_undispatched_attempts(source) else (source["status"], source["error"])


def executed_source_status(source):
    """统一当前和历史来源的实际结果判定。"""
    return executed_source_outcome(source)[0]


def cancelled_suffix_preserves_response(current, previous):
    """仅追加未派发预留时，除原始状态和预算汇总外不得改变来源事实。"""
    suffix = current["attempt_records"][len(previous["attempt_records"]):]
    if not suffix or any(a["status"] != "not_dispatched" for a in suffix):
        return True
    accounting = {"attempts", "attempt_records", "elapsed_seconds", "timing_unavailable_reason", "status", "error"}
    return ({k: v for k, v in current.items() if k not in accounting}
            == {k: v for k, v in previous.items() if k not in accounting})


def successor_sources_preserved(current, previous):
    """仅未完成的发现检查可以为原来源追加尝试。"""
    old_sources, new_sources = previous["sources"], current["sources"]
    if len(new_sources) < len(old_sources):
        return False
    retryable = {q["source_id"] for q in previous.get("discovery", {}).get("queries", [])
                 if previous["checks"][q["family"]]["status"] == "failed"}
    protected = {s for name, check in previous["checks"].items()
                 if name not in CHECKS or check["status"] != "failed" for s in check["source_ids"]}
    cancelled = {s["id"] for s in old_sources if only_undispatched_attempts(s)}
    checked_refs = {identity for check in previous["checks"].values()
                    if check["status"] in {"checked", "no_qualifying_items"} for identity in check["source_ids"]}
    retryable = (retryable - protected) | (cancelled - checked_refs)
    mutable = {"attempts", "attempt_records", "elapsed_seconds", "timing_unavailable_reason",
               "status", "error", "content_sha256", "retrieved_at"}
    for old, new in zip(old_sources, new_sources):
        if old == new:
            continue
        if old["id"] not in retryable or not cancelled_suffix_preserves_response(new, old):
            return False
        before, after = old["attempt_records"], new["attempt_records"]
        fields = mutable
        if only_undispatched_attempts(old) and old["published_at"] is None and any(a["status"] == "ok" for a in after[len(before):]):
            fields = mutable | {"published_at"}
        if {k: v for k, v in old.items() if k not in fields} != {k: v for k, v in new.items() if k not in fields}:
            return False
        if len(after) <= len(before) or after[:len(before)] != before or new["attempts"] != len(after):
            return False
        if old["elapsed_seconds"] is not None and new["elapsed_seconds"] is not None and new["elapsed_seconds"] < old["elapsed_seconds"]:
            return False
    return True


def repository(url):
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and p.hostname == "github.com" and not p.username
            and not p.password and port is None and not p.query and not p.fragment
            and re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", p.path)
            and all(x not in {".", ".."} for x in p.path[1:].split("/")))


def search_results(query, source, text, require):
    """解析真实 GET 响应；不把 HTML、错误 JSON 或偶然链接当作结果。"""
    p = urlsplit(source["url"])
    params = parse_qs(p.query)
    require(params.get("q") == [query["query"]], "Discovery query must match the actual request parameters")
    if query["provider"] == "github_search":
        require(p.hostname == "api.github.com" and p.path in {"/search/repositories", "/search/issues"}
                and params.get("per_page") == [str(query["result_limit"])]
                and params.get("page", ["1"]) == [str(query["page"])], "Bounded GitHub search endpoint required")
    else:
        require(p.hostname == "www.bing.com" and p.path == "/search" and params.get("format") == ["rss"]
                and params.get("count") == [str(query["result_limit"])] and query["page"] == 1
                and re.search(r'(?:^|\s)site:github\.com(?:\s|$)', query["query"].casefold()),
                "Bounded web search requires the actual Bing RSS endpoint")
    if source["status"] != "ok":
        require(not query["results"], "Failed discovery cannot claim assessed results")
        return False
    complete = True
    try:
        if query["provider"] == "github_search":
            response = json.loads(text)
            require(isinstance(response, dict) and type(response.get("total_count")) is int and response["total_count"] >= 0
                    and type(response.get("incomplete_results")) is bool and isinstance(response.get("items"), list),
                    "Discovery requires a structured bounded GitHub search response")
            complete = not response["incomplete_results"]
            urls = [item["html_url"] for item in response["items"]]
            require(all(isinstance(url, str) and urlsplit(url).hostname == "github.com" for url in urls), "Invalid GitHub search result URL")
            require(not complete or urls or response["total_count"] <= (query["page"] - 1) * query["result_limit"],
                    "Empty discovery page contradicts the reported result count")
            require(query["truncated"] == (not complete or response["total_count"] > len(urls)), "Discovery truncation must reflect the search response")
        else:
            require("<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper(), "Unsafe discovery XML")
            response = ET.fromstring(text)
            require(response.tag == "rss" and response.find("channel") is not None
                    and query["query"] in (response.findtext("channel/title") or ""), "Discovery requires a real query-bound RSS response")
            urls = [item.findtext("link") for item in response.findall("channel/item")]
            require(all(isinstance(url, str) and url.startswith(("https://", "http://")) for url in urls), "Invalid RSS result URL")
            require(query["truncated"] is True, "RSS cannot establish an exhaustive search total")
        require(len(urls) <= query["result_limit"] and query["result_limit"] <= 20, "Discovery result limit exceeded")
        require([r["url"] for r in query["results"]] == list(dict.fromkeys(urls)),
                "Every returned discovery result needs its original ordered disposition")
    except (ValueError, KeyError, TypeError, ET.ParseError) as exc:
        from .storage import BriefError
        if isinstance(exc, BriefError):
            raise
        require(False, "Invalid structured discovery response")
    return complete


def project_source(repo, url):
    p = urlsplit(url)
    prefix = urlsplit(repo).path.casefold()
    path = p.path.casefold()
    if p.hostname == "api.github.com":
        prefix = "/repos" + prefix
    return (p.scheme == "https" and p.hostname in {"github.com", "api.github.com", "raw.githubusercontent.com"}
            and (path == prefix or path.startswith(prefix + "/")))


def validate_discovery(metadata, sources, texts, candidates, history, require, time, read, previous=None):
    """验证发现读取；历史前缀只由调用者已核验的上轮审计提供。"""
    discovery = metadata["discovery"]
    prior_queries = previous["discovery"]["queries"] if previous is not None else []
    prior_attempts = {source["id"]: len(source["attempt_records"])
                      for source in previous["sources"]} if previous is not None else {}
    require(discovery["queries"][:len(prior_queries)] == prior_queries,
            "Discovery must preserve the authenticated previous query prefix")
    by_event = {c["event_key"]: c for c in candidates["items"]}
    decisions = {d["event_key"]: d for d in metadata["decisions"]}
    refs = {name: [] for name in CHECKS}
    ids, broad_queries = set(), {}
    operations, source_urls = set(), {}
    # 固定来源用途由原有覆盖检查和来源匹配共同确认，不依赖可被删去的查询行。
    fixed_sources = {identity for name in REQUIRED_CHECKS
                     for identity in metadata["checks"].get(name, {}).get("source_ids", [])
                     if identity in sources and source_matches(name, sources[identity]["url"])}
    cancelled_search_sources = set()
    for identity, source in sources.items():
        if not any(a["status"] == "not_dispatched" for a in source["attempt_records"]):
            continue
        parsed = urlsplit(source["url"])
        params = parse_qs(parsed.query)
        bounded = {str(value) for value in range(1, 21)}
        recognized = ((parsed.hostname == "api.github.com" and parsed.path in {"/search/repositories", "/search/issues"}
                       and len(params.get("per_page", [])) == 1 and params["per_page"][0] in bounded)
                      or (parsed.hostname == "www.bing.com" and parsed.path == "/search" and params.get("format") == ["rss"]
                          and len(params.get("count", [])) == 1 and params["count"][0] in bounded))
        query_values = params.get("q", [])
        if recognized and len(query_values) == 1:
            value = unquote(query_values[0]).casefold().replace('"', '').replace("'", "")
            fixed = re.search(r"(?:^|\s)[+-]?(?:repo|org|user|owner):|(?:github\.com|githubusercontent\.com)/[\w.-]", value)
            github_scope = parsed.hostname == "api.github.com" or re.search(r"(?:^|\s)site:github\.com(?:\s|$)", value)
            if github_scope and (not fixed or identity not in fixed_sources):
                cancelled_search_sources.add(identity)
    complete_queries = set()
    modes = {mode: [] for mode in ("current", "backscan", "followup")}
    as_of = time(metadata["as_of"])
    known_leads = {url for entry in history["entries"] for url in entry.get("source_urls", [])}
    for position, query in enumerate(discovery["queries"]):
        # 已认证的旧查询是历史取舍，三轮续页或纠错撤回后无需重新入选。
        # 当前元数据不能声明此豁免；新增查询仍绑定本轮候选与决策。
        retained = position < len(prior_queries)
        require(query["id"] not in ids, "Duplicate discovery query ID")
        ids.add(query["id"])
        modes[query["mode"]].append(query)
        since, until = time(query["since"]), time(query["until"])
        require(since < until == as_of, "Discovery window must end at the frozen as_of")
        if query["mode"] == "current":
            require(since == as_of - timedelta(hours=metadata["window_hours"]), "Current discovery must cover the research window")
            if query["source_id"] not in refs[query["family"]]:
                refs[query["family"]].append(query["source_id"])
        elif query["mode"] == "backscan":
            require(as_of - timedelta(days=90) <= since < as_of, "Discovery backscan is limited to 90 days")
        else:
            require(as_of - timedelta(days=90) <= since < as_of, "Discovery followup is limited to 90 days")
        if query["mode"] != "followup":
            value = unquote(query["query"]).casefold().replace('"', '').replace("'", '')
            require(not re.search(r"(?:^|\s)[+-]?(?:repo|org|user|owner):|(?:github\.com|githubusercontent\.com)/[\w.-]", value)
                    and query["followup_of"] is None,
                    "Discovery must search across repositories, not a known owner or project")
            identity = value
            require(broad_queries.get(identity, query["family"]) == query["family"], "One query cannot fill multiple discovery families")
            broad_queries[identity] = query["family"]
        else:
            require(query["followup_of"] in known_leads, "Followup must reference a retained discovery or report source")
        require(query["source_id"] in sources, "Discovery query needs an original budgeted source")
        source = sources[query["source_id"]]
        require(source_urls.get(source["url"], source["id"]) == source["id"],
                "Discovery retries must preserve the source ID and cumulative attempts")
        source_urls[source["url"]] = source["id"]
        operation = (source["id"], query["attempt"])
        require(operation not in operations and 1 <= query["attempt"] <= len(source["attempt_records"]),
                "Discovery must bind a distinct retained source attempt")
        operations.add(operation)
        attempt = source["attempt_records"][query["attempt"] - 1]
        fingerprint = query["response_sha256"]
        require(attempt["status"] != "not_dispatched" or fingerprint is None,
                "Undispatched discovery cannot retain response bytes")
        if attempt["status"] != "not_dispatched" and all(a["status"] == "not_dispatched"
                for a in source["attempt_records"][query["attempt"]:]):
            require(fingerprint == source["content_sha256"], "Discovery response must bind its actual final attempt")
        require(attempt["status"] != "ok" or fingerprint is not None, "Successful discovery needs original response bytes")
        text = ""
        if fingerprint is not None:
            if fingerprint == source["content_sha256"]:
                text = texts.get(source["id"], "")
            else:
                raw = read(f"evidence/{fingerprint}.txt")
                require(hashlib.sha256(raw).hexdigest() == fingerprint, "Retained discovery response fingerprint mismatch")
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    require(False, "Retained discovery response must be UTF-8")
        if search_results(query, dict(source, status=attempt["status"]), text, require):
            complete_queries.add(query["id"])
        urls = set()
        for result in query["results"]:
            require(result["url"] not in urls and result["url"] != source["url"],
                    "Discovery result must occur in the original search response")
            urls.add(result["url"])
            repo = result["repository_url"]
            require(repo is None or repository(repo), "Discovery repository must be a canonical project URL")
            require(repo is None or project_source(repo, result["url"]), "Search result and discovered repository differ")
            known_leads.add(result["url"])
            require(len(result["source_ids"]) == len(set(result["source_ids"])) and all(s in sources for s in result["source_ids"]),
                    "Discovery candidate evidence references must be unique and known")
            key = result["event_key"]
            if key is not None and not retained:
                require(key in by_event and key in decisions and decisions[key]["disposition"] == result["disposition"],
                        "Discovery candidate must preserve its actual selection decision")
            if result["disposition"] in {"selected", "update"}:
                require(key is not None and repo is not None and result["novelty"] in {"new_event", "new_evidence"},
                        "Old or unverified discovery cannot become today's news")
                linked = [sources[s] for s in result["source_ids"]]
                require(any(executed_source_status(s) == "ok" and project_source(repo, s["url"]) for s in linked)
                        and (retained or any(s["url"] == by_event[key]["source_url"] for s in linked)),
                        "Selected discovery needs project primary evidence and the candidate source")
                if not retained:
                    require(set(result["source_ids"]) <= set(decisions[key]["source_ids"]), "Discovery evidence must bind the selection audit")
                if result["novelty"] == "new_evidence":
                    require(result["baseline_url"] is not None and any(executed_source_status(s) == "ok" and s["url"] == result["baseline_url"] for s in linked),
                            "New evidence needs the retained previous baseline")
            if result["novelty"] == "old_discovery":
                require(result["disposition"] not in {"selected", "update"}, "Old discovery is a lead, not a new event")
            if result["disposition"] == "watch":
                require(repo is not None and result["next_step"].strip(), "Watch candidates need a project and a concrete followup")
    for identity in set(source_urls.values()) | cancelled_search_sources:
        cancelled = any(a["status"] == "not_dispatched" for a in sources[identity]["attempt_records"])
        require(all((identity, number) in operations for number, attempt in
                    enumerate(sources[identity]["attempt_records"], 1)
                    if attempt["status"] == "ok" or (cancelled and number > prior_attempts.get(identity, 0))),
                "Every retained discovery attempt needs its original query and response" if cancelled else
                "Every successful discovery attempt needs its original query and response")
    for mode in ("backscan", "followup"):
        note = discovery[mode]
        executed = [q for q in modes[mode] if sources[q["source_id"]]["attempt_records"][q["attempt"] - 1]["status"] != "not_dispatched"]
        require(len(executed) <= 2, "Auxiliary discovery must remain bounded")
        require((note["status"] == "performed") == bool(executed), "Discovery auxiliary work must match its actual queries")
    for name, source_ids in refs.items():
        require(len(source_ids) == len(set(source_ids)), "Discovery current queries must retain distinct operations")
        check = metadata["checks"][name]
        require(set(check["source_ids"]) == set(source_ids), "Discovery check must bind its current-window queries")
        if check["status"] == "not_checked":
            require(all(sources[q["source_id"]]["attempt_records"][q["attempt"] - 1]["status"] == "not_dispatched"
                        for q in modes["current"] if q["family"] == name),
                    "Executed discovery cannot be labeled not checked")
        if check["status"] == "failed":
            require(any(sources[q["source_id"]]["attempt_records"][q["attempt"] - 1]["status"] != "not_dispatched"
                        for q in modes["current"] if q["family"] == name),
                    "Failed discovery requires an executed current query")
        if check["status"] in {"checked", "no_qualifying_items"}:
            require(any(q["id"] in complete_queries for q in modes["current"] if q["family"] == name),
                    "Completed discovery needs a successful source read with complete bounded search results")
        if check["status"] == "no_qualifying_items":
            require(not any(r["disposition"] in {"selected", "update"} for q in modes["current"] if q["family"] == name for r in q["results"]),
                    "Discovery with selected results cannot claim no qualifying items")
    return refs
