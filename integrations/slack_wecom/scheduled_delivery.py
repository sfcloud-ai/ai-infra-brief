"""显式启用的 T4 队列核心，不自动创建服务、套接字或凭据。

可信适配器必须独立验证原报告包、来源提交、全部公开页面及原海报，返回精确计划。
不接受外部传入的 verified 标志。沿用原转发器的 Store、SQLite 及企微客户端，
不创建替代账本。
"""
import datetime as dt
import base64
import hashlib
import json
import os
import re
import math
import time
import uuid

if __package__:
    from . import relay
else:
    import relay


class ScheduledDeliveryError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def inspection_identity(request_id, report_date):
    """只读协议复用原请求身份；日期必须是规范的真实日历日期。"""
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", request_id):
        raise ScheduledDeliveryError("Bounded original request ID required")
    relay.notice_format.day_string(report_date)


def inspect_require(condition):
    if not condition:
        raise ScheduledDeliveryError("Scheduled inspection requires reconciliation")


def scheduled_notice_day(url, profile):
    """仅定时链路接受稳定日期键；旧原生连接器继续使用原版次契约。"""
    if isinstance(url, str):
        match = re.fullmatch(re.escape(profile["report_base_url"]) + r"(\d{4}-\d{2}-\d{2})/", url)
        if match:
            return relay.notice_format.day_string(match[1])
    return relay.notice_url_day(url, profile)


def validate_plan(plan, bundle, commit):
    fields = {"report_request_id", "report_date", "report_commit", "bundle_sha256", "notice_key", "text", "poster", "poster_sha256", "mime"}
    if not isinstance(plan, dict) or set(plan) != fields:
        raise ScheduledDeliveryError("Exact verified plan required")
    if (not isinstance(bundle, bytes) or not 0 < len(bundle) <= 8*1024*1024
            or not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or plan["report_commit"] != commit
            or plan["bundle_sha256"] != hashlib.sha256(bundle).hexdigest()):
        raise ScheduledDeliveryError("Source binding differs")
    day = dt.date.fromisoformat(plan["report_date"])
    if day.isoformat() != plan["report_date"]:
        raise ScheduledDeliveryError("Canonical report date required")
    if not isinstance(plan["text"], str) or not 0 < len(plan["text"].encode()) <= 32768:
        raise ScheduledDeliveryError("Bounded verified notice required")
    poster = plan["poster"]
    if (not isinstance(poster, bytes) or not 0 < len(poster) <= 2_000_000
            or plan["poster_sha256"] != hashlib.sha256(poster).hexdigest()):
        raise ScheduledDeliveryError("Original poster binding differs")
    relay.validate_image(poster, plan["mime"])
    # 北京时间 08:00 对应同一报告日期的 UTC 00:00。
    due = dt.datetime.combine(day, dt.time(), dt.timezone.utc).timestamp()
    record = {k: v for k, v in plan.items() if k != "poster"}
    return record, poster, due


def require_daily_migration(store, config):
    claims = {}
    for key,request_id in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'notice_edition:%'").fetchall():
        day = store.get("notice_edition_date:" + key.split(":",1)[1])
        if day is None:
            raise ScheduledDeliveryError("Historical edition date reconciliation required")
        if not store.db.execute("SELECT 1 FROM requests WHERE id=?",(request_id,)).fetchone():
            raise ScheduledDeliveryError("Historical daily claim conflict")
        claims.setdefault(day, set()).add(request_id)
    for day, owners in claims.items():
        day_key = "notice_day:" + relay.digest([config["team_id"],config["channel_id"],day])
        # 显式授权的新版重发可以持有日期声明；旧版次仍保留原请求和回执历史。
        if store.get(day_key) not in owners:
            raise ScheduledDeliveryError("Historical daily claim conflict")


def reconcile_daily_claims(store, config, verifier, snapshots):
    """停止旧消费者后执行的受控离线迁移。

    用独立验证的原报告包识别历史版次哈希键；缺项或同日冲突全部回滚。
    不修改旧请求结果，也不授权重试或发送。
    """
    mapped=[]
    for bundle,commit in snapshots:
        record,_,_=validate_plan(verifier(bundle,commit),bundle,commit)
        day=scheduled_notice_day(record["notice_key"],relay.validate_notice_connector(config["notice_connector"]))
        if day != record["report_date"]:raise ScheduledDeliveryError("Historical date differs")
        suffix=relay.digest([config["team_id"],config["channel_id"],record["notice_key"]])
        mapped.append((suffix,day))
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        for suffix,day in mapped:
            request_id=store.get("notice_edition:"+suffix)
            if request_id is None:raise ScheduledDeliveryError("Original edition claim absent")
            key="notice_day:"+relay.digest([config["team_id"],config["channel_id"],day])
            prior=store.get(key)
            if prior is not None and prior!=request_id:raise ScheduledDeliveryError("Conflicting historical day")
            store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",(key,request_id))
            store.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",("notice_edition_date:"+suffix,day))
        require_daily_migration(store,config)


class ScheduledDelivery:
    def __init__(self, store, config, verifier, wecom, clock=time.time, *, activation_date):
        self.store, self.db, self.config = store, store.db, config
        self.verify, self.wecom, self.clock = verifier, wecom, clock
        self.owner = uuid.uuid4().hex
        self.activation_date = dt.date.fromisoformat(activation_date).isoformat()
        if self.activation_date != activation_date:
            raise ScheduledDeliveryError("Explicit canonical activation boundary required")
        if not math.isfinite(clock()) or clock() <= 0:
            raise ScheduledDeliveryError("Finite clock required")
        relay.validate_notice_connector(config.get("notice_connector"))
        # 复用同一私有数据库；构造时不重置任何旧状态。
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_notices (
            request_id TEXT PRIMARY KEY, report_date TEXT UNIQUE NOT NULL,
            record TEXT NOT NULL, bundle BLOB NOT NULL, poster BLOB NOT NULL,
            due REAL NOT NULL, next_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, phase TEXT NOT NULL DEFAULT 'pending', last_error TEXT)""")
        self.db.commit()
        require_daily_migration(store, config)
        scope = relay.digest({"config":config,"activation_date":activation_date})
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            saved = store.get("scheduled_delivery_scope_v1")
            if saved is not None and saved != scope:
                raise ScheduledDeliveryError("Scheduled scope differs; reconcile before changing")
            if saved is None:
                self.db.execute("INSERT INTO meta VALUES (?,?)", ("scheduled_delivery_scope_v1",scope))

    def status(self, request_id):
        # 纯读取业务状态，包括未知结果，不触发预约或发送。
        row = self.db.execute("SELECT phase,next_at,attempts,last_error,record FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return {"status": "not_found"}
        record=json.loads(row[4])
        return {**self.store.status(request_id), "phase": row[0], "next_attempt_at": row[1], "attempts": row[2], "last_error": row[3], **{k:record[k] for k in ("report_date","report_commit","bundle_sha256")}}

    def inspect(self, request_id, report_date):
        """返回原账本的点时快照；不准入、联网核验、投递或修复历史记录。"""
        inspection_identity(request_id, report_date)
        inspect_require(not self.db.in_transaction)
        self.db.execute("BEGIN")
        try:
            observed_at = self.clock() * 1000
            inspect_require(math.isfinite(observed_at) and 0 < observed_at <= 9007199254740991)
            scope = self.store.get("scheduled_delivery_scope_v1")
            inspect_require(scope == relay.digest({"config": self.config, "activation_date": self.activation_date}))
            meta = dict(self._inspection_rows("SELECT key,value FROM meta WHERE key LIKE 'notice_edition:%' OR key LIKE 'notice_edition_date:%' OR key LIKE 'notice_day:%'", limit=10000))
            rows = self.db.execute("SELECT request_id,report_date FROM scheduled_notices WHERE request_id=? OR report_date=?", (request_id, report_date)).fetchall()
            inspect_require(len(rows) <= 1)
            scheduler = claim = None
            if rows:
                inspect_require(rows[0] == (request_id, report_date))
                scheduler = self._inspect_bound(request_id, report_date, meta)
                claim = {k: scheduler[k] for k in ("request_id", "report_date", "bundle_sha256", "report_commit")}
            else:
                self._inspect_absence(request_id, report_date, meta)
            return {"read_context": {"scheduler_scope_sha256": scope}, "observed_at": int(observed_at),
                    "complete": True, "scheduler_day_claim": claim, "scheduler": scheduler}
        finally:
            # 只释放读取快照，不提交调用方事务或更改任何业务行。
            self.db.rollback()

    def _inspection_rows(self, sql, arguments=(), *, limit=1000):
        rows = self.db.execute(sql + " LIMIT ?", (*arguments, limit + 1)).fetchall()
        inspect_require(len(rows) <= limit)
        return rows

    def _inspect_bound(self, request_id, report_date, meta, *, check_current_claim=True):
        row = self.db.execute("SELECT record,bundle,poster,due,phase,next_at,attempts,last_error FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()
        inspect_require(row is not None)
        encoded, bundle, poster, due, phase, next_at, attempts, error = row
        record = json.loads(encoded, object_pairs_hook=relay.notice_format._unique_object)
        checked, _, expected_due = validate_plan({**record, "poster": poster}, bundle, record["report_commit"])
        inspect_require(canonical(checked) == encoded and record["report_request_id"] == request_id
                        and record["report_date"] == report_date and due == expected_due)
        base = relay.validate_notice_connector(self.config["notice_connector"])["report_base_url"]
        inspect_require(scheduled_notice_day(record["notice_key"], self.config["notice_connector"]) == report_date)
        source = json.loads(bundle.decode("utf-8"), object_pairs_hook=relay.notice_format._unique_object)
        inspect_require(source.get("request_id") == request_id and isinstance(source.get("files"), list))
        metadata = [item for item in source["files"] if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].endswith("/report.json")]
        inspect_require(len(metadata) == 1)
        report_bytes = base64.b64decode(metadata[0]["base64"], validate=True)
        report = json.loads(report_bytes.decode("utf-8"), object_pairs_hook=relay.notice_format._unique_object)
        revision = report.get("revision")
        inspect_require(type(revision) in (int, float) and math.isfinite(revision)
                        and 0 < revision <= 9007199254740991 and int(revision) == revision)
        manifest = relay.notice_format.build_manifest({**report, "revision": int(revision)}, base,
                                                      hashlib.sha256(report_bytes).hexdigest(), record["report_commit"],
                                                      stable=record["notice_key"] == f"{base}{report_date}/")
        inspect_require(manifest["index_url"] == record["notice_key"] and manifest["text"] == record["text"]
                        and manifest["poster"]["sha256"] == record["poster_sha256"]
                        and relay.notice_format.bundle_poster(bundle, manifest, base) == poster)
        if check_current_claim:
            self._inspect_claim(request_id, report_date, record["notice_key"], meta)
        original = self.db.execute("SELECT mode,intent_hash,status,parts_total,parts_sent FROM requests WHERE id=?", (request_id,)).fetchone()
        inspect_require(original is not None and original[:2] == ("scheduled", relay.digest(record)))
        _, _, status, total, sent = original
        pieces = relay.chunks(record["text"])
        parts = self.db.execute("SELECT part,content_hash,status FROM parts WHERE request_id=? ORDER BY part", (request_id,)).fetchall()
        images = self.db.execute("SELECT file_id,ordinal,status,content_hash,byte_size,error_code FROM image_parts WHERE request_id=? ORDER BY ordinal", (request_id,)).fetchall()
        inspect_require(len(parts) == len(pieces) and len(images) == 1)
        states = {"pending", "sending", "sent", "failed", "unknown"}
        inspect_require(all(p[0] == i and p[1] == hashlib.sha256(pieces[i].encode()).hexdigest() and p[2] in states for i, p in enumerate(parts)))
        image = images[0]
        inspect_require(image[:2] == ("scheduled-poster", len(pieces)) and image[2] in states
                        and image[3:5] == (record["poster_sha256"], len(poster)) and image[5] is None)
        actual_sent = sum(p[2] == "sent" for p in parts) + int(image[2] == "sent")
        inspect_require(type(total) is int and total == len(parts) + 1 and type(sent) is int and sent == actual_sent
                        and status in {"queued", "sending", "sent", "failed", "unknown"}
                        and phase in {"pending", "verifying", "sending", "unknown", "complete"})
        inspect_require((status == "sent") == (phase == "complete") == (sent == total))
        actual_states = [p[2] for p in parts] + [image[2]]
        inspect_require((phase not in {"pending", "verifying"} or status in {"queued", "failed"})
                        and (phase != "unknown" or status == "unknown")
                        and (phase != "sending" or status in {"sending", "unknown"})
                        and ("unknown" not in actual_states or status == "unknown")
                        and ("sending" not in actual_states or phase == "sending")
                        and (status != "unknown" or "unknown" in actual_states)
                        and (status != "sending" or "sending" in actual_states))
        inspect_require(type(next_at) in (int, float) and math.isfinite(next_at) and next_at > 0
                        and type(attempts) is int and 0 <= attempts <= 9007199254740991
                        and (error is None or isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", error)))
        return {"request_id": request_id, "report_date": report_date, "bundle_sha256": record["bundle_sha256"],
                "report_commit": record["report_commit"], "status": status, "parts_total": total, "parts_sent": sent,
                "phase": phase, "next_attempt_at": next_at, "attempts": attempts, "last_error": error,
                "images": [{"file_id": image[0], "status": image[2], "error": image[5]}]}

    def _inspect_claim(self, request_id, report_date, notice_key, meta):
        suffix = relay.digest([self.config["team_id"], self.config["channel_id"], notice_key])
        day_key = "notice_day:" + relay.digest([self.config["team_id"], self.config["channel_id"], report_date])
        inspect_require(meta.get(day_key) == request_id and meta.get("notice_edition:" + suffix) == request_id
                        and meta.get("notice_edition_date:" + suffix) == report_date)
        for key, day in meta.items():
            if key.startswith("notice_edition_date:") and day == report_date:
                inspect_require(meta.get("notice_edition:" + key.split(":", 1)[1]) == request_id)

    def _inspect_completed_handoff(self, request_id, report_date, owner, meta):
        """已完成定时记录可由显式手动重发接替；旧记录和接替回执必须均完整。"""
        original = self._inspect_bound(request_id, report_date, meta, check_current_claim=False)
        inspect_require(original["status"] == "sent" and original["phase"] == "complete")
        record = json.loads(self.db.execute("SELECT record FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()[0])
        suffix = relay.digest([self.config["team_id"], self.config["channel_id"], record["notice_key"]])
        day_key = "notice_day:" + relay.digest([self.config["team_id"], self.config["channel_id"], report_date])
        inspect_require(meta.get(day_key) == owner and meta.get("notice_edition_date:" + suffix) == report_date
                        and "notice_edition:" + suffix in meta)
        owners = {meta.get("notice_edition:" + key.split(":", 1)[1]) for key, day in meta.items()
                  if key.startswith("notice_edition_date:") and day == report_date}
        inspect_require(owner in owners)
        # 换版手动重发只接管新版本；旧版本可继续指向已完成的原始或手动回执。
        for edition_owner in owners - {request_id}:
            self._inspect_completed_manual_owner(request_id, edition_owner)

    def _inspect_completed_manual_owner(self, request_id, owner):
        row = self.db.execute("SELECT mode,status,parts_total,parts_sent,reason_hash,message_ts,source_key,created FROM requests WHERE id=?", (owner,)).fetchone()
        inspect_require(row is not None)
        mode, status, total, sent, reason, timestamp, source, created = row
        previous = self.db.execute("SELECT created FROM requests WHERE id=?", (request_id,)).fetchone()[0]
        inspect_require(mode == "manual_slack" and status == "sent"
                        and type(total) is int and total > 0 and type(sent) is int and sent == total
                        and isinstance(reason, str) and re.fullmatch(r"[a-f0-9]{64}", reason)
                        and isinstance(timestamp, str) and relay.TS.fullmatch(timestamp)
                        and source == relay.digest([self.config["team_id"], self.config["channel_id"], timestamp])
                        and type(created) in (int, float) and math.isfinite(created)
                        and type(previous) in (int, float) and math.isfinite(previous) and created >= previous)
        parts = self.db.execute("SELECT part,content_hash,status FROM parts WHERE request_id=? ORDER BY part", (owner,)).fetchall()
        images = self.db.execute("SELECT ordinal,content_hash,status,byte_size,error_code FROM image_parts WHERE request_id=? ORDER BY ordinal", (owner,)).fetchall()
        inspect_require(parts and len(parts) + len(images) == total
                        and all(part == i and state == "sent" and isinstance(digest, str)
                                and re.fullmatch(r"[a-f0-9]{64}", digest) for i, (part, digest, state) in enumerate(parts))
                        and all(ordinal == i and state == "sent" and isinstance(digest, str)
                                and re.fullmatch(r"[a-f0-9]{64}", digest) and type(size) is int and size > 0
                                and error is None for i, (ordinal, digest, state, size, error) in enumerate(images)))

    def _inspect_absence(self, request_id, report_date, meta):
        """缺失目标不是空日证明；未归属的旧声明和孤儿记录均阻断。"""
        inspect_require(self.db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone() is None)
        owners, expected_days = {}, {}
        for key, owner in meta.items():
            inspect_require(re.fullmatch(r"notice_(?:edition|edition_date|day):[a-f0-9]{64}", key) is not None)
            if not key.startswith("notice_edition:"):
                continue
            day = meta.get("notice_edition_date:" + key.split(":", 1)[1])
            relay.notice_format.day_string(day)
            inspect_require(self.db.execute("SELECT 1 FROM requests WHERE id=?", (owner,)).fetchone() is not None)
            owners.setdefault(day, set()).add(owner)
        for key in meta:
            if key.startswith("notice_edition_date:"):
                inspect_require("notice_edition:" + key.split(":", 1)[1] in meta)
        for day, ids in owners.items():
            key = "notice_day:" + relay.digest([self.config["team_id"], self.config["channel_id"], day])
            inspect_require(meta.get(key) in ids)
            expected_days[key] = meta[key]
        inspect_require({k: v for k, v in meta.items() if k.startswith("notice_day:")} == expected_days
                        and report_date not in owners)
        for table in ("parts", "image_parts"):
            inspect_require(self.db.execute("SELECT 1 FROM " + table + " p LEFT JOIN requests r ON r.id=p.request_id WHERE r.id IS NULL LIMIT 1").fetchone() is None)
        inspect_require(self.db.execute("SELECT 1 FROM requests r WHERE r.mode!='scheduled' AND (r.status IN ('sending','unknown') OR EXISTS(SELECT 1 FROM parts p WHERE p.request_id=r.id AND p.status IN ('sending','unknown')) OR EXISTS(SELECT 1 FROM image_parts p WHERE p.request_id=r.id AND p.status IN ('sending','unknown'))) LIMIT 1").fetchone() is None)
        inspect_require(self.db.execute("SELECT 1 FROM requests r LEFT JOIN scheduled_notices q ON q.request_id=r.id WHERE r.mode='scheduled' AND q.request_id IS NULL LIMIT 1").fetchone() is None)
        for rid, day, encoded in self._inspection_rows("SELECT request_id,report_date,record FROM scheduled_notices"):
            record = json.loads(encoded, object_pairs_hook=relay.notice_format._unique_object)
            inspection_identity(rid, day)
            inspect_require(record.get("report_request_id") == rid and record.get("report_date") == day and canonical(record) == encoded)
            original = self.db.execute("SELECT mode,intent_hash,status FROM requests WHERE id=?", (rid,)).fetchone()
            inspect_require(original is not None and original[:2] == ("scheduled", relay.digest(record)))
            day_key = "notice_day:" + relay.digest([self.config["team_id"], self.config["channel_id"], day])
            owner = meta.get(day_key)
            if owner is not None and owner != rid:
                self._inspect_completed_handoff(rid, day, owner, meta)
            else:
                self._inspect_claim(rid, day, record["notice_key"], meta)
            # 原定时未知结果可隔离，但必须仍有完整且一致的原绑定。
            uncertain_parts = self.db.execute("SELECT 1 FROM parts WHERE request_id=? AND status IN ('sending','unknown') UNION SELECT 1 FROM image_parts WHERE request_id=? AND status IN ('sending','unknown') LIMIT 1", (rid, rid)).fetchone()
            if original[2] in {"sending", "unknown"} or uncertain_parts:
                self._inspect_bound(rid, day, meta)

    def admit(self, request_id, bundle, commit):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", request_id):
            raise ScheduledDeliveryError("Bounded request ID required")
        record, poster, due = validate_plan(self.verify(bundle, commit), bundle, commit)
        if record["report_date"] < self.activation_date:
            raise ScheduledDeliveryError("Historical reports require separate reconciliation")
        if record["report_request_id"] != request_id:
            raise ScheduledDeliveryError("Original bundle request differs")
        day = scheduled_notice_day(record["notice_key"], relay.validate_notice_connector(self.config["notice_connector"]))
        if day != record["report_date"]:
            raise ScheduledDeliveryError("Notice date differs")
        pieces = relay.chunks(record["text"])
        edition = "notice_edition:" + relay.digest([self.config["team_id"], self.config["channel_id"], record["notice_key"]])
        day_key = "notice_day:" + relay.digest([self.config["team_id"], self.config["channel_id"], record["report_date"]])
        encoded = canonical(record)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            require_daily_migration(self.store,self.config)
            prior = self.db.execute("SELECT record,bundle,poster FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior != (encoded, bundle, poster):
                    raise ScheduledDeliveryError("Original request binding conflict")
                return self.status(request_id)
            if (self.store.get(day_key) is not None or self.store.get(edition) is not None
                    or self.db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone()
                    or self.db.execute("SELECT 1 FROM scheduled_notices WHERE report_date=?", (record["report_date"],)).fetchone()):
                raise ScheduledDeliveryError("Existing report or attempt must be reconciled")
            self.db.execute("INSERT INTO scheduled_notices(request_id,report_date,record,bundle,poster,due,next_at) VALUES (?,?,?,?,?,?,?)", (request_id,record["report_date"],encoded,bundle,poster,due,due))
            self.db.execute("INSERT INTO requests(id,intent_hash,mode,actor_uid,created,status,parts_total) VALUES (?,?,?, ?,?,?,?)", (request_id,relay.digest(record),"scheduled",os.getuid(),self.clock(),"queued",len(pieces)+1))
            self.db.execute("INSERT INTO meta VALUES (?,?)", (edition, request_id))
            self.db.execute("INSERT INTO meta VALUES (?,?)", (day_key, request_id))
            self.db.execute("INSERT INTO meta VALUES (?,?)", ("notice_edition_date:"+edition.split(":",1)[1],record["report_date"]))
            for i, text in enumerate(pieces):
                self.db.execute("INSERT INTO parts VALUES (?,?,?,?)", (request_id,i,hashlib.sha256(text.encode()).hexdigest(),"pending"))
            self.db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status,content_hash,byte_size) VALUES (?,?,?,?,?,?)", (request_id,"scheduled-poster",len(pieces),"pending",record["poster_sha256"],len(poster)))
        return self.status(request_id)

    def tick(self):
        now = self.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("""SELECT request_id,record,bundle,poster FROM scheduled_notices
                WHERE next_at<=? AND due<=? AND (phase='pending' OR (phase='verifying' AND lease_until<=?))
                ORDER BY due LIMIT 1""", (now,now,now)).fetchone()
            if row is None:
                return None
            request_id, encoded, bundle, poster = row
            self.db.execute("UPDATE scheduled_notices SET phase='verifying',owner=?,lease_until=? WHERE request_id=?", (self.owner,now+180,request_id))
        started = self.clock()
        try:
            record = json.loads(encoded)
            checked, image, _ = validate_plan(self.verify(bundle,record["report_commit"]),bundle,record["report_commit"])
            if canonical(checked) != encoded or image != poster:
                raise ScheduledDeliveryError("Live publication or stored content differs")
        except Exception as error:
            # 尚未发送，保留原绑定并延期重试验证。
            self._defer(request_id,reason="publication_mismatch" if isinstance(error,ScheduledDeliveryError) else "publication_read_failed")
            return self.status(request_id)
        pieces = relay.chunks(record["text"])
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("SELECT phase,owner,lease_until FROM scheduled_notices WHERE request_id=?", (request_id,)).fetchone()
            if job is None or job[:2] != ("verifying",self.owner):
                return self.status(request_id)
            if not started <= self.clock() < min(started+60,job[2]):
                self._defer_locked(request_id, "verifying", "verification_expired")
                return self.status(request_id)
            if self.clock() < float(self.store.get("next_send", "0")):
                self.db.execute("UPDATE scheduled_notices SET phase='pending',next_at=? WHERE request_id=?", (float(self.store.get("next_send")), request_id))
                return self.status(request_id)
            parts = self.db.execute("SELECT part,status,content_hash FROM parts WHERE request_id=? ORDER BY part", (request_id,)).fetchall()
            images = self.db.execute("SELECT status,content_hash FROM image_parts WHERE request_id=? AND file_id='scheduled-poster'", (request_id,)).fetchone()
            if (len(parts) != len(pieces) or [p[0] for p in parts] != list(range(len(pieces)))
                    or images is None or any(hashlib.sha256(pieces[i].encode()).hexdigest() != h for i,_,h in parts)):
                raise ScheduledDeliveryError("Incomplete or corrupted part ledger")
            if any(s not in ("sent","pending","failed") for _,s,_ in parts) or images[0] not in ("sent","pending","failed"):
                self.db.execute("UPDATE scheduled_notices SET phase='unknown' WHERE request_id=?", (request_id,))
                return self.status(request_id)
            pending = next(((i,h) for i,s,h in parts if s != "sent"),None)
            if pending:
                index, hashed = pending
                if hashlib.sha256(pieces[index].encode()).hexdigest() != hashed:
                    raise ScheduledDeliveryError("Stored text part differs")
                self.db.execute("UPDATE parts SET status='sending' WHERE request_id=? AND part=?", (request_id,index))
            else:
                index = None
                if images[0] == "sent":
                    self.db.execute("UPDATE scheduled_notices SET phase='complete' WHERE request_id=?",(request_id,))
                    self.db.execute("UPDATE requests SET status='sent' WHERE id=?",(request_id,))
                    return self.status(request_id)
                if images[1] != hashlib.sha256(poster).hexdigest():
                    raise ScheduledDeliveryError("Stored image differs")
                self.db.execute("UPDATE image_parts SET status='sending' WHERE request_id=? AND file_id='scheduled-poster'", (request_id,))
            # 共享原转发器限速状态，不另建发送配额。
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("next_send", str(self.clock()+3.2)))
            # 此事务提交后不再允许接管发送，即使进程重启。
            self.db.execute("UPDATE scheduled_notices SET phase='sending' WHERE request_id=?",(request_id,))
            self.db.execute("UPDATE requests SET status='sending' WHERE id=?",(request_id,))
        try:
            outcome = self.wecom.send(pieces[index]) if index is not None else self.wecom.send_image(poster,record["mime"])
        except Exception:
            outcome = "unknown"
        if outcome not in ("sent","failed","unknown"):
            outcome = "unknown"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if index is not None:
                self.db.execute("UPDATE parts SET status=? WHERE request_id=? AND part=?",(outcome,request_id,index))
            else:
                self.db.execute("UPDATE image_parts SET status=? WHERE request_id=? AND file_id='scheduled-poster'",(outcome,request_id))
            if outcome == "sent":
                self.db.execute("UPDATE requests SET parts_sent=parts_sent+1 WHERE id=?",(request_id,))
            remaining = self.db.execute("SELECT count(*) FROM parts WHERE request_id=? AND status!='sent'", (request_id,)).fetchone()[0]
            remaining += self.db.execute("SELECT count(*) FROM image_parts WHERE request_id=? AND status!='sent'", (request_id,)).fetchone()[0]
            complete = outcome == "sent" and remaining == 0
            phase = "complete" if complete else "unknown" if outcome == "unknown" else "pending"
            status = "sent" if complete else outcome if outcome != "sent" else "queued"
            self.db.execute("UPDATE scheduled_notices SET phase=?,next_at=? WHERE request_id=?",(phase,self.clock()+3.2,request_id))
            self.db.execute("UPDATE requests SET status=? WHERE id=?",(status,request_id))
        if outcome == "failed":
            self._defer(request_id,phase="pending",reason="wecom_explicit_failure")
        return self.status(request_id)

    def _defer(self, request_id, phase="verifying", reason="publication_read_failed"):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._defer_locked(request_id, phase, reason)

    def _defer_locked(self, request_id, phase, reason):
        # 调用方持有 BEGIN IMMEDIATE，包括上面的验证时效门禁。
        row = self.db.execute("SELECT attempts FROM scheduled_notices WHERE request_id=? AND phase=? AND owner=?",(request_id,phase,self.owner)).fetchone()
        if row:
            attempts = row[0]+1
            delay = min(3600,15*(2**min(attempts-1,8)))
            self.db.execute("UPDATE scheduled_notices SET phase='pending',attempts=?,next_at=?,last_error=? WHERE request_id=?",(attempts,self.clock()+delay,reason,request_id))
