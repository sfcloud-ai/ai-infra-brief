"""只读核对 Slack 通知；不转发消息，也不改变转发器状态。"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__:
    from . import relay, notice_format
else:
    import relay
    import notice_format


class NoticeCheckError(ValueError):
    """实际读取的通知不符合预期投递契约。"""


def read_request(data, config):
    """请求仅提供预期计划，不能指定身份或声称已经完成认证读取。"""
    if not isinstance(data, bytes) or not 0 < len(data) <= relay.MAX_BODY:
        raise NoticeCheckError("Bounded notice check request required")
    request = json.loads(data.decode("utf-8"))
    fields = {"message_ts", "file_id", "notice_manifest"}
    if (not isinstance(request, dict) or set(request) not in (fields, fields | {"transport", "bundle_sha256", "bundle_bytes"})
            or not isinstance(request["message_ts"], str) or not relay.TS.fullmatch(request["message_ts"])
            or not isinstance(request["file_id"], str) or not re.fullmatch(r"F[A-Z0-9]+", request["file_id"])):
        raise NoticeCheckError("Exact message, file and expected notice manifest required")
    if "transport" in request and (request["transport"] != "report_bundle_v1"
            or not isinstance(request["bundle_sha256"], str) or not notice_format.HASH.fullmatch(request["bundle_sha256"])
            or type(request["bundle_bytes"]) is not int or not 0 < request["bundle_bytes"] <= notice_format.MAX_BUNDLE_BYTES):
        raise NoticeCheckError("Exact bundle transport digest and length required")
    profile = config.get("notice_connector")
    relay.validate_notice_connector(profile)
    if (config.get("forward_images") is not True or profile["user_id"] != config.get("user_id")
            or profile["user_id"] == config.get("sender_id")
            or not isinstance(config.get("team_id"), str) or not re.fullmatch(r"T[A-Z0-9]+", config["team_id"])
            or not isinstance(config.get("channel_id"), str) or not re.fullmatch(r"D[A-Z0-9]+", config["channel_id"])):
        raise NoticeCheckError("Existing connector and final DM configuration required")
    notice_format.validate_manifest(request["notice_manifest"], profile["report_base_url"])
    return request


def exact_message(slack, timestamp):
    body = slack.history(oldest=timestamp, latest=timestamp, inclusive="true", limit=2)
    messages = body.get("messages") if isinstance(body, dict) else None
    if (not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict)
            or messages[0].get("ts") != timestamp or body.get("has_more")
            or body.get("response_metadata", {}).get("next_cursor")):
        raise NoticeCheckError("Complete unique exact-message read required")
    return deepcopy(messages[0])


def read_received_notice(data, config, slack, *, notice_reader=None):
    """认证后回读消息并校验原文件；不调用投递或状态写入接口。"""
    request = read_request(data, config)
    if slack.config != config:
        raise NoticeCheckError("Slack client scope differs from local configuration")
    slack.verify()
    profile, manifest = config["notice_connector"], request["notice_manifest"]
    message = exact_message(slack, request["message_ts"])
    notice = relay.connector_notice(message, profile, config["team_id"], config["channel_id"],
                                   notice_manifest=manifest)
    if notice["images"][0]["file_id"] != request["file_id"]:
        raise NoticeCheckError("Observed file differs from reserved file")
    reader = notice_reader or notice_format.NoticeManifestReader(profile["report_base_url"])
    published = reader.read(notice)
    notice_format.validate_manifest(published, profile["report_base_url"])
    # 后续整站部署可以保留同一期不可变报告；逐项比较所有内容字段，
    # 包括报告与海报摘要，不要求整站提交与原报告提交相同。
    content = lambda value: {k: v for k, v in value.items() if k != "commit"}
    if content(published) != content(manifest):
        raise NoticeCheckError("Published notice content differs from expected edition")
    image = notice["images"][0]
    if request.get("transport") != image.get("transport"):
        raise NoticeCheckError("Observed transport differs from reservation")
    transport = None
    if request.get("transport") == "report_bundle_v1":
        transport = slack.download_bundle(image)
        if (not isinstance(transport, bytes) or len(transport) != request["bundle_bytes"]
                or hashlib.sha256(transport).hexdigest() != request["bundle_sha256"]):
            raise NoticeCheckError("Actual bundle differs from reserved source")
        poster = relay.original_bundle_poster(transport, image)
    else:
        poster = slack.download_image(image)
    plan = {"notice_manifest": manifest,
            "event_template": {"text": manifest["text"], "files": [{"id": request["file_id"]}]}}
    if transport is not None:
        plan.update({k: request[k] for k in ("transport", "bundle_sha256", "bundle_bytes")})
    result = check_received_notice(plan, message, channel_id=config["channel_id"],
                                   sender_id=profile["user_id"], team_id=config["team_id"],
                                   read_context={"channel_id": config["channel_id"], "team_id": config["team_id"]},
                                   notice_connector=profile, actual_poster_bytes=poster, actual_transport_bytes=transport)
    if exact_message(slack, request["message_ts"]) != message or reader.read(notice) != published:
        raise NoticeCheckError("Notice or publication changed during verification")
    return {**result, "observed_publication_commit": published["commit"],
            "messages_sent": 0, "state_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read and verify an existing Slack notice; never sends")
    parser.add_argument("--root", required=True, type=Path, help="Existing private relay configuration root")
    args = parser.parse_args(argv)
    try:
        config = relay.load_config(args.root)
        data = sys.stdin.buffer.read(relay.MAX_BODY + 1)
        read_request(data, config)  # 读取凭据之前先拒绝无效请求。
        slack = relay.Slack(relay.load_secrets(args.root)["slack_user_token"], config)
        result = read_received_notice(data, config, slack)
    except Exception:
        # 不记录原始消息、下载地址、令牌、Webhook 或底层网络错误。
        print(json.dumps({"slack_status": "not_verified", "wecom_status": "not_checked",
                          "error": "notice_read_unverified", "messages_sent": 0, "state_changed": False}))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


def check_received_notice(plan, message, *, channel_id, sender_id, team_id, read_context, notice_connector=None, actual_poster_bytes=None, actual_transport_bytes=None):
    """核对实际读取的 Slack 顶层图文；不把匹配规则当作企微送达回执。"""
    if __package__:
        from . import relay
        from .images import select_images
    else:
        import relay
        from images import select_images

    if (not all(isinstance(value, str) and value for value in (channel_id, sender_id, team_id))
            or read_context != {"channel_id": channel_id, "team_id": team_id}):
        raise NoticeCheckError("Authenticated Slack read context differs from expected destination")
    template = plan.get("event_template", {})
    if not isinstance(message, dict) or not isinstance(template.get("text"), str):
        raise NoticeCheckError("Expected a notice template and received Slack message")
    ts = message.get("ts")
    def poster_receipt(image):
        if plan.get("notice_manifest") is None:
            return {}
        try:
            expected = plan["notice_manifest"]["poster"]["sha256"]
            bundled = image.get("transport") == "report_bundle_v1"
            if plan.get("transport") != image.get("transport"):
                raise relay.ImageError("notice_transport_mismatch")
            if not isinstance(actual_poster_bytes, bytes) or (not bundled and len(actual_poster_bytes) != image["size"]):
                raise relay.ImageError("notice_poster_bytes_required")
            relay.validate_image(actual_poster_bytes, image["mime"])
            actual_hash = hashlib.sha256(actual_poster_bytes).hexdigest()
            if actual_hash != expected:
                raise relay.ImageError("notice_poster_hash_mismatch")
            if bundled:
                if (not isinstance(actual_transport_bytes, bytes) or len(actual_transport_bytes) != image["size"]
                        or len(actual_transport_bytes) != plan["bundle_bytes"]
                        or hashlib.sha256(actual_transport_bytes).hexdigest() != plan["bundle_sha256"]
                        or relay.original_bundle_poster(actual_transport_bytes, image) != actual_poster_bytes):
                    raise relay.ImageError("notice_bundle_hash_mismatch")
                return {"file_id": image["file_id"], "transport_sha256": plan["bundle_sha256"],
                        "transport_bytes": len(actual_transport_bytes), "poster_sha256": actual_hash,
                        "poster_bytes": len(actual_poster_bytes)}
        except (relay.ImageError, KeyError, TypeError) as error:
            raise NoticeCheckError("Actual Slack poster must exactly match published source bytes") from error
        return {"file_id": image["file_id"], "transport_sha256": actual_hash,
                "transport_bytes": len(actual_poster_bytes)}

    if notice_connector is not None:
        try:
            if notice_connector.get("user_id") != sender_id:
                raise relay.RelayError("untrusted_notice")
            notice = relay.connector_notice(message, notice_connector, team_id, channel_id,
                                           notice_manifest=plan.get("notice_manifest"))
        except (relay.RelayError, AttributeError) as error:
            raise NoticeCheckError("Slack connector notice contract failed") from error
        wanted = template.get("files", [])
        if (notice["text"] != relay.plain_text(template["text"])
                or len(wanted) != 1 or not wanted[0].get("id")
                or notice["images"][0]["file_id"] != wanted[0]["id"]):
            raise NoticeCheckError("Slack connector title, URL or information poster differs from plan")
        return {"slack_status": "verified", "wecom_status": "not_checked", "publication_status": "not_checked",
                "receipt": {"channel": channel_id, "ts": ts, "relay_request_id": "auto:" + ts,
                            **poster_receipt(notice["images"][0])}}
    receiver = relay.Relay(None, None, None, {"sender_id": sender_id})
    if (message.get("channel", channel_id) != channel_id or message.get("team", team_id) != team_id
            or not isinstance(ts, str) or not relay.TS.fullmatch(ts)
            or message.get("hidden") or message.get("type", "message") != "message"
            or message.get("subtype") != "file_share" or not receiver.is_brief(message)):
        raise NoticeCheckError("Slack identity, title or top-level file-share contract failed")
    expected = relay.plain_text(template["text"])
    blocks = message.get("blocks", [])
    if message.get("attachments") or (isinstance(blocks, list) and any(
            isinstance(block, dict) and "accessory" in block for block in blocks)):
        raise NoticeCheckError("Slack notice contains unsupported visible accessories or attachments")
    if "blocks" in message and message["blocks"] != []:
        # 空 fallback 强制检查 blocks 自身，不能用 fallback 冒充可见正文。
        if not isinstance(message["blocks"], list) or relay.message_text({**message, "text": ""}) != expected:
            raise NoticeCheckError("Slack visible blocks are incomplete or unverifiable")
    actual = relay.message_text(message)
    if actual != expected or len(relay.chunks(actual)) != 1:
        raise NoticeCheckError("Slack visible subtitle or complete URL differs from the single-part plan")
    files, wanted = message.get("files"), template.get("files", [])
    selected = select_images(message, team_id)
    if (not isinstance(files, list) or len(files) != 1 or len(wanted) != 1 or not wanted[0].get("id")
            or len(selected) != 1 or "error" in selected[0] or selected[0].get("file_id") != wanted[0]["id"]):
        raise NoticeCheckError("Slack direct information poster differs from plan")
    return {"slack_status": "verified", "wecom_status": "not_checked", "publication_status": "not_checked",
            "receipt": {"channel": channel_id, "ts": ts, "relay_request_id": "auto:" + ts,
                        **poster_receipt(selected[0])}}


if __name__ == "__main__":
    raise SystemExit(main())
