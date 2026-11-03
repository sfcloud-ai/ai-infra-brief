"""显式启用的 T4 队列核心，不自动创建服务、套接字或凭据。

可信适配器必须独立验证原报告包、来源提交、全部公开页面及原海报，返回精确计划。
不接受外部传入的 verified 标志。沿用原转发器的 Store、SQLite 及企微客户端，
不创建替代账本。
"""
import datetime as dt
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
        day=relay.notice_url_day(record["notice_key"],relay.validate_notice_connector(config["notice_connector"]))
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

    def admit(self, request_id, bundle, commit):
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", request_id):
            raise ScheduledDeliveryError("Bounded request ID required")
        record, poster, due = validate_plan(self.verify(bundle, commit), bundle, commit)
        if record["report_date"] < self.activation_date:
            raise ScheduledDeliveryError("Historical reports require separate reconciliation")
        if record["report_request_id"] != request_id:
            raise ScheduledDeliveryError("Original bundle request differs")
        day = relay.notice_url_day(record["notice_key"], relay.validate_notice_connector(self.config["notice_connector"]))
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
            if job is None or job[:2] != ("verifying",self.owner) or not started <= self.clock() < min(started+60,job[2]):
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
            row = self.db.execute("SELECT attempts FROM scheduled_notices WHERE request_id=? AND phase=? AND owner=?",(request_id,phase,self.owner)).fetchone()
            if row:
                attempts = row[0]+1
                delay = min(3600,15*(2**min(attempts-1,8)))
                self.db.execute("UPDATE scheduled_notices SET phase='pending',attempts=?,next_at=?,last_error=? WHERE request_id=?",(attempts,self.clock()+delay,reason,request_id))
