"""核验未引用的备用 PDF 所对应的原始 GET 回执。"""
import hashlib
import re
from html import unescape
from urllib.parse import urldefrag

from .source_identity import source_identity
from .storage import BriefError


def mentions_source(value, identity, url):
    if isinstance(value, dict):
        return any(mentions_source(item, identity, url) for item in value.values())
    if isinstance(value, list):
        return any(mentions_source(item, identity, url) for item in value)
    if not isinstance(value, str):
        return False
    if value == identity:
        return True
    for link in re.findall(r"""https?://[^\s<>"'\[\]()]+""", unescape(value), re.I):
        try:
            if source_identity(urldefrag(link.rstrip(".,;!?。，；")).url) == source_identity(url):
                return True
        except ValueError:
            continue
    return False


def require_uncited(source, *uses):
    if any(mentions_source(use, source["id"], source["url"]) for use in uses):
        raise BriefError("Archive followup cannot supply candidate, coverage or status evidence")


def validate_archive_followup(source, metadata, candidates, read, parse):
    ticket = source["archive_followup"]
    identity, url = source["id"], source["url"]
    attempts = source.get("attempt_records", [])
    if len(attempts) != 1 or source["status"] != "ok" or source["attempts"] != 1 or attempts[0]["status"] != "ok":
        raise BriefError("Archive followup needs one successful original GET")
    attempt = attempts[0]
    for other in metadata["sources"]:
        if other["id"] != identity and source_identity(other["url"]) == source_identity(url):
            raise BriefError("Archive followup cannot share an evidence source URL")
    uses = [metadata.get(key) for key in ("checks", "discovery", "status_entries", "decisions", "page_scope")]
    require_uncited(source, *uses, candidates)

    raw = read(f"stage_artifacts/{ticket['result_sha256']}.json")
    if hashlib.sha256(raw).hexdigest() != ticket["result_sha256"]:
        raise BriefError("Archive followup receipt fingerprint mismatch")
    result = parse(raw, "archive followup GET receipt")
    if not isinstance(result, dict) or not isinstance(result.get("artifacts"), dict):
        raise BriefError("Invalid archive followup GET receipt")
    body = result["artifacts"].get("response.body.bin")
    transfer = result.get("transfer")
    if not isinstance(body, dict) or not isinstance(transfer, dict) or not isinstance(transfer.get("content_type"), str):
        raise BriefError("Invalid archive followup body metadata")
    if not (result.get("operation_id") == ticket["operation_id"]
            and result.get("request_id") == "morning-" + metadata["date"].replace("-", "") + "-r1"
            and result.get("source_id") == identity and result.get("url") == url
            and result.get("input_sha256") == source_identity(url)["hash"]
            and result.get("content_sha256") == body.get("sha256") == ticket["body_sha256"] == source["content_sha256"]
            and body.get("bytes") == ticket["bytes"] and result.get("response_complete") is True
            and result.get("dispatch_started") is True and result.get("attempts_in_this_operation") == 1
            and result.get("status") == "ok" and result.get("error") == "" and result.get("curl_exit_code") == 0
            and result.get("http_status") == 200 and result.get("coverage_status") == "not_assessed"
            and transfer["content_type"].split(";")[0].strip().lower() == "application/pdf"
            and result.get("reserved_seconds") == attempt["reserved_seconds"]
            and result.get("elapsed_seconds") == attempt["elapsed_seconds"] == source["elapsed_seconds"]):
        raise BriefError("Archive followup does not match the original successful PDF receipt")
