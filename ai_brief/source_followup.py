"""从原始来源回执生成下一批受预算约束的读取请求。"""
from collections import defaultdict
from datetime import datetime
from email.message import Message
from hashlib import sha256
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit
from .source_identity import canonical_source_url, source_identity


class PreflightError(ValueError):
    pass


class URLInputError(PreflightError):
    pass


def require(condition, message):
    if not condition:
        raise PreflightError(message)


def timestamp(value):
    require(isinstance(value, str), "Timestamp must have a timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError("Invalid timestamp") from exc
    require(result.utcoffset() is not None, "Timestamp must have a timezone")
    return result


def url_key(value):
    if not isinstance(value, str):
        raise URLInputError("Source URL is missing")
    if any(char.isspace() for char in value):
        raise URLInputError("Invalid source URL")
    try:
        return canonical_source_url(value)
    except ValueError as exc:
        raise URLInputError("Invalid source URL") from exc


def next_links(headers, base):
    unfolded = re.sub(r"\r?\n[ \t]+", " ", headers.decode("latin1"))
    for value in re.findall(r"(?im)^link:[ \t]*([^\r\n]*)", unfolded):
        for match in re.finditer(r'<([^>]+)>((?:"(?:\\.|[^"\\])*"|[^",])*)', value):
            params = Message()
            params["Content-Type"] = "application/octet-stream" + match[2]
            relations = params.get_param("rel", "")
            if isinstance(relations, str) and "next" in relations.lower().split():
                yield urljoin(base, match[1])


def listing_identity(url):
    parts = urlsplit(url_key(url))
    position = {"cursor"} if parts.hostname == "huggingface.co" else {"page"}
    filters = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in position]
    return parts.scheme, parts.netloc, parts.path, sorted(filters)


def verified_cutoff(url, body, next_urls, lower):
    """只接受已知按时间降序的原始结构化列表，不能用任意日期摘录停止。"""
    parts = urlsplit(url_key(url))
    params = dict(parse_qsl(parts.query))
    try:
        rows = json.loads(body)
    except ValueError as exc:
        raise PreflightError("Pagination cutoff needs the original structured listing") from exc
    field = None
    if parts.hostname == "api.github.com" and re.fullmatch(r"/(?:repos/[^/]+/[^/]+|repositories/\d+)/pulls", parts.path):
        if params.get("sort") == "updated" and params.get("direction") == "desc":
            field = "updated_at"
    elif parts.hostname == "api.github.com" and parts.path == "/search/issues":
        if (params.get("sort") == "updated" and params.get("order", "desc") == "desc"
                and isinstance(rows, dict) and rows.get("incomplete_results") is False):
            field, rows = params["sort"] + "_at", rows.get("items")
    elif parts.hostname == "huggingface.co" and parts.path == "/api/models":
        if params.get("sort") == "lastModified" and params.get("direction") == "-1":
            field = "lastModified"
    require(field is not None and isinstance(rows, list) and rows,
            "Pagination cutoff requires a supported original descending time order")
    dates = [timestamp(row.get(field)) for row in rows if isinstance(row, dict)]
    require(len(dates) == len(rows) and all(a >= b for a, b in zip(dates, dates[1:])) and dates[-1] < lower,
            "Pagination has not passed the frozen lower cutoff")
    require(all(same_listing(url, link) for link in next_urls),
            "Cutoff cannot discard a different listing or article")


OFFICIAL_ALTERNATIVES = {
    "qwen.ai": ("https://qwen.ai/api/v2/article/retrieval?language=en-US&type=qwen_ai",
                "https://qwen.ai/blog?id=qwen4-max",
                "https://www.alibabacloud.com/en/press-room/alibaba-unveils-qwen4-max"),
    "z.ai": ("https://www.zhipuai.cn/zh/research", "https://docs.z.ai/release-notes/new-released.md"),
}


def same_listing(original, followup):
    first, second = listing_identity(original), listing_identity(followup)
    if first == second:
        return True
    if not (first[:2] == second[:2] == ("https", "api.github.com") and first[3] == second[3]):
        return False
    # GitHub 在响应头中用数字 ID 表示仓库或组织。
    for named, numeric in ((r"/repos/[^/]+/[^/]+", r"/repositories/[0-9]+"), (r"/orgs/[^/]+", r"/organizations/[0-9]+")):
        a, b = re.fullmatch(named + r"(/.*)", first[2]), re.fullmatch(numeric + r"(/.*)", second[2])
        if a and b and a[1] == b[1]:
            return True
    return False


def github_events(url, body):
    if urlsplit(url).hostname != "api.github.com":
        return
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return
    rows = data.get("items", [data]) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        link = row.get("html_url")
        if isinstance(link, str) and re.fullmatch(r"https://github\.com/[^/]+/[^/]+/(?:pull/[0-9]+|commit/[a-f0-9]{40})", link):
            yield link.casefold()


def source_followup(ledger_path, operations_dir, since):
    """从已结算回执生成 GET 身份，实际读取沿用原请求授权。"""
    try:
        return _source_followup(ledger_path, operations_dir, since)
    except PreflightError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PreflightError("Malformed native source ledger or receipt") from exc


def _source_followup(ledger_path, operations_dir, since):
    ledger = json.loads(ledger_path.read_bytes())
    lower = timestamp(since)
    require(lower <= timestamp(ledger["scheduled_for"]), "Lower cutoff exceeds the scheduled run")
    require(operations_dir.is_dir() and not operations_dir.is_symlink(), "Original operations directory required")
    source_ops = [op for op in ledger["operations"] if op["kind"] == "source"]
    ops = {op["operation_id"]: op for op in source_ops}
    require(len(ops) == len(source_ops) and all(op["operation_key"] == op["input_hash"] for op in source_ops),
            "Source operation identity differs")
    attempted = {op["input_hash"] for op in ops.values()}
    observed, pages, candidates, bodies, events, gaps = {}, [], {}, defaultdict(list), defaultdict(set), []
    numeric_paths = defaultdict(set)
    def propose(url, reason):
        identity = source_identity(url)
        if identity["hash"] not in attempted:
            candidates.setdefault(identity["hash"], {"input": identity["value"], "reason": reason})
    for path in sorted(operations_dir.glob("*/result.json")):
        op = ops.get(path.parent.name)
        if not op or op["phase"] != "acknowledged":
            continue
        require(not path.is_symlink() and not path.parent.is_symlink(), "Linked source receipt")
        raw = path.read_bytes()
        result = json.loads(raw)
        oid = result["operation_id"]
        url = result["url"]
        require(result["request_id"] == ledger["request_id"] and oid == path.parent.name and op["result_file"] == sha256(raw).hexdigest()
                and op["input_hash"] == result["input_sha256"] == source_identity(url)["hash"], "Source operation differs")
        retained = {}
        for name in ("response.body.bin", "response.headers.bin"):
            file = path.parent / name
            require(not file.is_symlink(), "Linked source evidence")
            body = file.read_bytes()
            descriptor = result["artifacts"][name]
            require(len(body) == descriptor["bytes"] and sha256(body).hexdigest() == descriptor["sha256"],
                    "Source evidence hash differs")
            retained[name] = body
        host = urlsplit(url_key(url)).hostname.removeprefix("www.")
        route = urlsplit(url).path.rstrip("/")
        if (host, route) in {("qwen.ai", "/research"), ("z.ai", "/blog"), ("z.ai", "/sitemap.xml")}:
            for alternative in OFFICIAL_ALTERNATIVES[host]:
                propose(alternative, "official_cross_check")
        ok = result["status"] == "ok" and result["response_complete"] is True and not result["recording_errors"]
        ok = bool(ok and isinstance(result["http_status"], int) and 200 <= result["http_status"] < 300)
        observed[oid] = (url_key(url), ok)
        if not ok:
            gaps.append({"operation_id": oid, "reason": "source_read_failed"})
            continue
        headers = re.split(rb"(?m)^(?=HTTP/\d[^\s]*\s+\d{3}\b)", retained["response.headers.bin"])[-1]
        require(re.match(rb"HTTP/\d[^\s]*[ \t]+" + str(result["http_status"]).encode() + rb"\b", headers),
                "Final HTTP status differs")
        effective = result.get("transfer", {}).get("url_effective") or url
        links = sorted(set(next_links(headers, effective)))
        if len(links) > 1:
            gaps.append({"operation_id": oid, "reason": "ambiguous_next_link"})
            links = []
        body = retained["response.body.bin"]
        for event in github_events(effective, body):
            events[event].add(oid)
        bodies[result["artifacts"]["response.body.bin"]["sha256"]].append(oid)
        cutoff = False
        hf_listing = urlsplit(effective).hostname == "huggingface.co" and urlsplit(effective).path == "/api/models"
        if links or hf_listing:
            try:
                verified_cutoff(effective, body, links, lower)
                cutoff = True
            except PreflightError:
                pass
        if hf_listing and not links and not cutoff:
            params = dict(parse_qsl(urlsplit(effective).query))
            try:
                values = json.loads(body)
                saturated = not isinstance(values, list) or len(values) >= int(params.get("limit", 1000))
            except (TypeError, ValueError):
                saturated = True
            if saturated:
                gaps.append({"operation_id": oid, "reason": "model_listing_without_verified_end"})
        for link in links:
            if not same_listing(effective, link):
                gaps.append({"operation_id": oid, "url": link, "reason": "listing_identity_changed"})
            else:
                origin, target = urlsplit(effective), urlsplit(link)
                if origin.hostname == "api.github.com" and target.path != origin.path:
                    numeric_paths[target.path].add(origin.path)
                if origin.hostname == "api.github.com" and origin.path in {"/search/issues", "/search/repositories"}:
                    page = [v for k, v in parse_qsl(target.query, keep_blank_values=True) if k == "page"]
                    if page not in (["1"], ["2"]):
                        gaps.append({"operation_id": oid, "url": link, "reason": "discovery_page_limit"})
                        continue
                pages.append((oid, url_key(link), cutoff))
    def page_key(url):
        parts = urlsplit(url)
        names = numeric_paths.get(parts.path)
        if parts.hostname == "api.github.com" and names and len(names) == 1:
            return parts._replace(path=next(iter(names))).geturl()
        return url
    good = {page_key(url) for url, ok in observed.values() if ok}
    observed_pages = {page_key(url) for url, ok in observed.values()}
    edges = {page_key(observed[oid][0]): page_key(url) for oid, url, cutoff in pages if not cutoff}
    visited = set()
    for start in edges:
        page, trail = start, {}
        while page in edges and page not in visited and page not in trail:
            trail[page] = len(trail)
            page = edges[page]
        if page in trail:
            gaps.append({"urls": sorted(list(trail)[trail[page]:]), "reason": "pagination_cycle"})
        visited.update(trail)
    proposed_pages = set()
    for oid, url, cutoff in pages:
        key = page_key(url)
        if not cutoff and key not in good:
            if key in observed_pages or any(source_identity(candidate)["hash"] in attempted for candidate in {key, url}):
                gaps.append({"operation_id": oid, "url": url, "reason": "next_response_unverified"})
            elif key not in proposed_pages:
                propose(url, "pagination")
                proposed_pages.add(key)
    for oid, op in ops.items():
        if op["phase"] != "acknowledged":
            gaps.append({"operation_id": oid, "reason": "source_operation_unsettled"})
        elif oid not in observed:
            gaps.append({"operation_id": oid, "reason": "retained_receipt_missing"})
    return {"request_id": ledger["request_id"], "since": since, "verified_receipts": len(observed),
            "next_requests": list(candidates.values()), "gaps": gaps,
            "identical_bodies": {key: ids for key, ids in bodies.items() if len(ids) > 1},
            "repeated_events": {key: sorted(ids) for key, ids in events.items() if len(ids) > 1},
            "pagination": [{"operation_id": oid, "next": url, "window_cutoff": cutoff,
                            "next_read": page_key(url) in good} for oid, url, cutoff in pages]}
