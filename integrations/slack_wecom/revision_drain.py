"""停机/排空时只读检查接续链，不能将 superseded 当作原消息已发送。"""
import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def require(value):
    if not value:
        raise ValueError("unreconciled_revision_supersession")


def validate_superseded(db, request_id):
    seen = set()
    for _ in range(101):
        require(request_id not in seen); seen.add(request_id)
        original = db.execute("SELECT record,phase FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()
        if original is None:
            original = db.execute("SELECT record,phase FROM scheduled_revision_notices WHERE request_id=?", (request_id,)).fetchone()
        require(original is not None and original[1] == "superseded")
        link = db.execute("SELECT successor,original_record_hash,original_parts FROM scheduled_supersessions WHERE predecessor=?", (request_id,)).fetchone()
        parts = db.execute("SELECT part,content_hash,status FROM parts WHERE request_id=? ORDER BY part", (request_id,)).fetchall()
        images = db.execute("SELECT file_id,content_hash,status,byte_size,ordinal FROM image_parts WHERE request_id=? ORDER BY ordinal", (request_id,)).fetchall()
        require(link is not None and link[1] == hashlib.sha256(original[0].encode()).hexdigest()
                and link[2] == canonical({"parts": parts, "images": images})
                and all(p[2] in {"pending", "failed", "sent"} for p in parts + images))
        request_id = link[0]
        successor = db.execute("SELECT record,bundle,poster,phase FROM scheduled_revision_notices WHERE request_id=?", (request_id,)).fetchone()
        require(successor is not None)
        if successor[3] == "superseded":
            continue
        record = json.loads(successor[0]); plan = record["plan"]
        request = db.execute("SELECT intent_hash,mode,status,parts_total,parts_sent FROM requests WHERE id=?", (request_id,)).fetchone()
        parts = db.execute("SELECT part,content_hash,status FROM parts WHERE request_id=? ORDER BY part", (request_id,)).fetchall()
        images = db.execute("SELECT file_id,content_hash,status,byte_size,ordinal FROM image_parts WHERE request_id=? ORDER BY ordinal", (request_id,)).fetchall()
        require(canonical(record) == successor[0] and hashlib.sha256(successor[1]).hexdigest() == plan["bundle_sha256"]
                and hashlib.sha256(successor[2]).hexdigest() == plan["poster_sha256"]
                and plan["report_request_id"] == request_id and request is not None
                and request[:2] == (hashlib.sha256(canonical(record).encode()).hexdigest(), "scheduled_revision"))
        if successor[3] == "skipped":
            require(record["delivery_kind"] == "web_only" and record["text"] == "" and request[2:] == ("skipped", 0, 0) and not parts and not images)
        else:
            # 全部修订短文和首次标题列表均受 1900-byte 合同约束，为一条文字。
            require(successor[3] == "complete" and request[2] == "sent" and len(parts) == 1
                    and record["delivery_kind"] in {"initial", "revision_update"}
                    and parts[0] == (0, hashlib.sha256(record["text"].encode()).hexdigest(), "sent")
                    and len(images) == int(record["delivery_kind"] == "initial")
                    and request[3] == request[4] == len(parts) + len(images))
            if images:
                require(images[0] == ("scheduled-poster", plan["poster_sha256"], "sent", len(successor[2]), 1))
        return
    raise ValueError("revision_supersession_chain_limit")
