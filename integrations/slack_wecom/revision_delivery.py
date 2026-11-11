"""同一 relay 进程、连接和发送账本中的修订队列，保留每次原始意图。"""
import hashlib
import json
import math
import os
import re

if __package__:
    from . import relay
    from .revision_notice import validate_notice, update_text
else:
    import relay
    from revision_notice import validate_notice, update_text


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RevisionDelivery:
    def __init__(self, parent, validate_plan):
        self.parent, self.db, self.validate = parent, parent.db, validate_plan
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_revision_notices (
            request_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, revision INTEGER NOT NULL,
            record TEXT NOT NULL, bundle BLOB NOT NULL, poster BLOB NOT NULL,
            due REAL NOT NULL, next_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, phase TEXT NOT NULL, last_error TEXT,
            UNIQUE(report_date,revision))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduled_supersessions (
            predecessor TEXT PRIMARY KEY, successor TEXT NOT NULL, original_record_hash TEXT NOT NULL,
            original_phase TEXT NOT NULL, original_parts TEXT NOT NULL)""")
        self.db.commit()

    def row(self, request_id):
        return self.db.execute("SELECT record,bundle,poster,phase,due,next_at,attempts,last_error,report_date,revision,owner,lease_until FROM scheduled_revision_notices WHERE request_id=?", (request_id,)).fetchone()

    def parts(self, request_id):
        return self.db.execute("SELECT part,content_hash,status FROM parts WHERE request_id=? ORDER BY part", (request_id,)).fetchall()

    def images(self, request_id):
        return self.db.execute("SELECT file_id,content_hash,status,byte_size,ordinal FROM image_parts WHERE request_id=? ORDER BY ordinal", (request_id,)).fetchall()

    def started(self, request_id):
        request = self.db.execute("SELECT status,parts_sent FROM requests WHERE id=?", (request_id,)).fetchone()
        return bool(request and (request[0] in {"sending", "sent", "unknown"} or request[1] > 0)
                    or any(p[2] not in {"pending", "failed", "not_attempted"} for p in self.parts(request_id) + self.images(request_id)))

    def status(self, request_id):
        row = self.row(request_id)
        if row is None:
            return None
        record = json.loads(row[0]); plan = record["plan"]
        result = {**self.parent.store.status(request_id), **{k: plan[k] for k in ("report_date", "report_commit", "bundle_sha256")},
                  "phase": row[3], "next_attempt_at": row[5], "attempts": row[6], "last_error": row[7],
                  "delivery_kind": record["delivery_kind"], "report_revision": plan["revision_notice"]["revision"]}
        result["covered_requests"] = [item["request_id"] for item in record["covered"]]
        if row[3] == "superseded":
            self.check_supersession(request_id, row[0])
            result["superseded_by"] = self.db.execute("SELECT successor FROM scheduled_supersessions WHERE predecessor=?", (request_id,)).fetchone()[0]
        return result

    def admit(self, request_id, plan, poster, bundle, due):
        notice = validate_notice(plan["revision_notice"])
        day = plan["report_date"]
        if request_id != f"morning-{day.replace('-', '')}-r{notice['revision']}":
            raise ValueError("canonical_revision_request_required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.row(request_id)
            if prior:
                if (json.loads(prior[0])["plan"], prior[1], prior[2]) != (plan, bundle, poster):
                    raise ValueError("revision_request_conflict")
                return self.status(request_id)
            if self.db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone():
                raise ValueError("revision_request_already_reserved")
            revisions = self.db.execute("SELECT request_id,revision,record,phase FROM scheduled_revision_notices WHERE report_date=? ORDER BY revision", (day,)).fetchall()
            if len(revisions) >= 100:
                raise ValueError("revision_day_capacity_reached")
            if revisions and revisions[-1][1] >= notice["revision"]:
                raise ValueError("revision_must_advance")
            original = self.db.execute("SELECT request_id,record,phase FROM scheduled_notices WHERE report_date=?", (day,)).fetchone()
            day_key = "notice_day:" + relay.digest([self.parent.config["team_id"], self.parent.config["channel_id"], day])
            day_owner = self.parent.store.get(day_key)
            if day_owner and not original and not any(r[0] == day_owner for r in revisions) and not self.started(day_owner):
                raise ValueError("legacy_initial_reconciliation_required")
            started = (bool(day_owner and self.started(day_owner))
                       or bool(original and self.started(original[0]))
                       or any(self.started(row[0]) for row in revisions))
            covered = []
            effective = notice
            for rid, revision, encoded, phase in revisions:
                old = json.loads(encoded)
                if phase in {"pending", "verifying"} and not self.started(rid):
                    covered.append({"request_id": rid, "record_sha256": hashlib.sha256(encoded.encode()).hexdigest()})
                    if notice["kind"] == "maintenance" and old["delivery_kind"] == "revision_update":
                        effective = old["effective_notice"]
            kind = "initial" if not started else "web_only" if effective["kind"] == "maintenance" else "revision_update"
            # 同一重要说明若已进入发送边界，后续版次不能借新 ID 重试未知结果。
            base = relay.validate_notice_connector(self.parent.config["notice_connector"])["report_base_url"]
            text = plan["text"] if kind == "initial" else update_text(day, effective, base) if kind == "revision_update" else ""
            if kind == "revision_update" and any(phase in {"sending", "unknown"} and json.loads(encoded)["text"] == text for _, _, encoded, phase in revisions):
                kind, text = "web_only", ""
            record = {"plan": plan, "delivery_kind": kind, "text": text, "effective_notice": effective, "covered": covered}
            encoded = canonical(record)
            phase = "skipped" if kind == "web_only" else "pending"
            retry_at, attempts, last_error = due, 0, None
            for item in covered:
                old_row = self.row(item['request_id'])
                if json.loads(old_row[0])['text'] == text:
                    retry_at = max(retry_at, old_row[5])
                    if old_row[6] >= attempts:
                        attempts, last_error = old_row[6], old_row[7]
            if original and original[2] in {'pending', 'verifying'} and json.loads(original[1])['text'] == text:
                old_retry = self.db.execute('SELECT next_at,attempts,last_error FROM scheduled_notices WHERE request_id=?', (original[0],)).fetchone()
                retry_at = max(retry_at, old_retry[0])
                if old_retry[1] >= attempts:
                    attempts, last_error = old_retry[1], old_retry[2]
            self.db.execute("INSERT INTO scheduled_revision_notices(request_id,report_date,revision,record,bundle,poster,due,next_at,phase,attempts,last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (request_id, day, notice["revision"], encoded, bundle, poster, due, retry_at, phase, attempts, last_error))
            pieces = relay.chunks(text) if text else []
            self.db.execute("INSERT INTO requests(id,intent_hash,mode,actor_uid,created,status,parts_total) VALUES (?,?,?,?,?,?,?)",
                            (request_id, relay.digest(record), "scheduled_revision", os.getuid(), self.parent.clock(), "skipped" if not pieces else "queued", len(pieces) + int(kind == "initial")))
            for i, piece in enumerate(pieces):
                self.db.execute("INSERT INTO parts VALUES (?,?,?,'pending')", (request_id, i, hashlib.sha256(piece.encode()).hexdigest()))
            if kind == "initial":
                self.db.execute("INSERT INTO image_parts(request_id,file_id,ordinal,status,content_hash,byte_size) VALUES (?,?,?,'pending',?,?)",
                                (request_id, "scheduled-poster", len(pieces), plan["poster_sha256"], len(poster)))
            candidates = [("scheduled_notices", *original)] if original else []
            candidates += [("scheduled_revision_notices", rid, encoded, phase) for rid, _, encoded, phase in revisions]
            for table, rid, old_encoded, old_phase in candidates:
                if old_phase not in {"pending", "verifying"}:
                    continue
                # 与 sender 最后的 sending CAS 使用同一个 BEGIN IMMEDIATE；
                # 撤销租约后迟到的旧验证结果不能恢复发送。
                parts = canonical({"parts": self.parts(rid), "images": self.images(rid)})
                self.db.execute("INSERT INTO scheduled_supersessions VALUES (?,?,?,?,?)",
                                (rid, request_id, hashlib.sha256(old_encoded.encode()).hexdigest(), old_phase, parts))
                self.db.execute(f"UPDATE {table} SET phase='superseded',owner=NULL,lease_until=NULL WHERE request_id=?", (rid,))
            if day_owner is None:
                edition = "notice_edition:" + relay.digest([self.parent.config["team_id"], self.parent.config["channel_id"], plan["notice_key"]])
                self.db.execute("INSERT INTO meta VALUES (?,?)", (day_key, request_id))
                self.db.execute("INSERT INTO meta VALUES (?,?)", (edition, request_id))
                self.db.execute("INSERT INTO meta VALUES (?,?)", ("notice_edition_date:" + edition.split(":", 1)[1], day))
            return self.status(request_id)

    def check_record(self, request_id):
        row = self.row(request_id)
        if row is None:
            raise ValueError("revision_record_missing")
        record = json.loads(row[0]); plan = record["plan"]
        if set(record) != {"plan", "delivery_kind", "text", "effective_notice", "covered"}:
            raise ValueError("revision_record_fields_differ")
        validate_notice(record["effective_notice"])
        checked, poster, due = self.validate({**plan, "poster": row[2]}, row[1], plan["report_commit"])
        original = self.db.execute("SELECT mode,intent_hash,status,parts_total,parts_sent FROM requests WHERE id=?", (request_id,)).fetchone()
        if (canonical(record) != row[0] or checked != plan or due != row[4] or plan["report_request_id"] != request_id
                or row[8] != plan["report_date"] or row[9] != plan["revision_notice"]["revision"]
                or request_id != f"morning-{row[8].replace('-', '')}-r{row[9]}"
                or type(row[5]) not in (int, float) or not math.isfinite(row[5]) or row[5] <= 0
                or type(row[6]) is not int or not 0 <= row[6] <= 9007199254740991
                or row[7] is not None and (not isinstance(row[7], str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", row[7]) is None)
                or original is None or original[:2] != ("scheduled_revision", relay.digest(record))):
            raise ValueError("revision_binding_differs")
        base = relay.validate_notice_connector(self.parent.config["notice_connector"])["report_base_url"]
        kind = record["delivery_kind"]
        expected = plan["text"] if kind == "initial" else update_text(plan["report_date"], record["effective_notice"], base) if kind == "revision_update" else ""
        pieces = relay.chunks(expected) if expected else []
        parts, images = self.parts(request_id), self.images(request_id)
        if (kind not in {"initial", "revision_update", "web_only"} or record["text"] != expected
                or len(parts) != len(pieces) or any(p[0] != i or p[1] != hashlib.sha256(pieces[i].encode()).hexdigest() for i, p in enumerate(parts))
                or len(images) != int(kind == "initial")
                or images and (images[0][0] != "scheduled-poster" or images[0][1] != plan["poster_sha256"] or images[0][3:] != (len(poster), len(parts)))
                or original[3] != len(parts) + len(images) or original[4] != sum(p[2] == "sent" for p in parts + images)):
            raise ValueError("revision_part_binding_differs")
        states = [p[2] for p in parts + images]
        if (any(s not in {"pending", "failed", "sending", "sent", "unknown"} for s in states)
                or row[3] not in {"pending", "verifying", "sending", "unknown", "complete", "skipped", "superseded"}
                or (row[3] == "skipped") != (kind == "web_only")
                or (original[2] == "sent") != (row[3] == "complete")
                or (row[3] == "complete") != (bool(states) and all(s == "sent" for s in states))):
            raise ValueError("revision_state_differs")
        phase, status = row[3], original[2]
        if ((phase in {"pending", "verifying", "superseded"} and (status not in {"queued", "failed"} or any(s in {"sending", "unknown"} for s in states)))
                or (phase == "unknown" and (status != "unknown" or "unknown" not in states or "sending" in states))
                or (phase == "sending" and not (status == "sending" and "sending" in states or status == "unknown" and "unknown" in states))
                or (phase == "skipped" and status != "skipped")
                or (phase in {"verifying", "sending"} and (not isinstance(row[10], str) or re.fullmatch(r"[a-f0-9]{32}", row[10]) is None
                    or type(row[11]) not in (int, float) or not math.isfinite(row[11]) or row[11] <= 0))):
            raise ValueError("revision_state_differs")
        day_key = "notice_day:" + relay.digest([self.parent.config["team_id"], self.parent.config["channel_id"], row[8]])
        owner = self.parent.store.get(day_key)
        if not owner or not self.db.execute("SELECT 1 FROM requests WHERE id=?", (owner,)).fetchone():
            raise ValueError("revision_day_claim_missing")
        if kind == "initial":
            seen = set()
            while owner != request_id and owner not in seen and len(seen) <= 100:
                seen.add(owner)
                link = self.db.execute("SELECT successor FROM scheduled_supersessions WHERE predecessor=?", (owner,)).fetchone()
                if link is None:
                    break
                owner = link[0]
            if owner != request_id:
                if row[3] == 'superseded':
                    if __package__:
                        from .revision_drain import validate_superseded
                    else:
                        from revision_drain import validate_superseded
                    validate_superseded(self.db, request_id)
                elif row[3] != 'complete':
                    raise ValueError("revision_initial_claim_differs")
                meta = dict(self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'notice_%'"))
                original_day = self.db.execute('SELECT record FROM scheduled_notices WHERE report_date=?', (row[8],)).fetchone()
                original_key = json.loads(original_day[0])['notice_key'] if original_day else plan['notice_key']
                self.parent._inspect_completed_owner_claims(request_id, row[8], original_key, self.parent.store.get(day_key), meta)
        if not isinstance(record["covered"], list) or len(record["covered"]) > 100:
            raise ValueError("revision_coverage_limit")
        if row[3] == "superseded":
            self.check_supersession(request_id, row[0])
        for covered in record["covered"]:
            if not isinstance(covered, dict) or set(covered) != {"request_id", "record_sha256"}:
                raise ValueError("revision_coverage_fields")
            old = self.row(covered["request_id"])
            link = self.db.execute("SELECT successor FROM scheduled_supersessions WHERE predecessor=?", (covered["request_id"],)).fetchone()
            if old is None or old[3] != "superseded" or link != (request_id,) or hashlib.sha256(old[0].encode()).hexdigest() != covered["record_sha256"]:
                raise ValueError("revision_coverage_binding_differs")
        return row, record

    def inspect(self, request_id, day):
        row, record = self.check_record(request_id)
        if record["delivery_kind"] != "initial" or record["plan"]["report_date"] != day:
            raise ValueError("revision_initial_day_binding_differs")
        status = self.status(request_id)
        fields = ("request_id", "report_date", "report_commit", "bundle_sha256", "status", "phase", "parts_total", "parts_sent", "next_attempt_at", "attempts", "last_error")
        return {**{key: status[key] for key in fields},
                "images": [{"file_id": image[0], "status": image[2], "error": None} for image in self.images(request_id)]}

    def assert_frozen(self, request_id):
        row, record = self.check_record(request_id)
        states = [p[2] for p in self.parts(request_id) + self.images(request_id)]
        if row[3] not in {"sending", "unknown"} or not any(s in {"sending", "unknown"} for s in states):
            raise ValueError("revision_unknown_not_frozen")

    def check_supersession(self, request_id, encoded):
        value = self.db.execute("SELECT successor,original_record_hash,original_parts FROM scheduled_supersessions WHERE predecessor=?", (request_id,)).fetchone()
        if (value is None or value[1] != hashlib.sha256(encoded.encode()).hexdigest()
                or value[2] != canonical({"parts": self.parts(request_id), "images": self.images(request_id)})
                or self.row(value[0]) is None):
            raise ValueError("supersession_binding_differs")

    def tick(self):
        now = self.parent.clock()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            found = self.db.execute("SELECT request_id FROM scheduled_revision_notices WHERE due<=? AND next_at<=? AND (phase='pending' OR (phase='verifying' AND lease_until<=?)) ORDER BY due,revision LIMIT 1", (now, now, now)).fetchone()
            if not found:
                return None
            rid = found[0]
            self.db.execute("UPDATE scheduled_revision_notices SET phase='verifying',owner=?,lease_until=? WHERE request_id=?", (self.parent.owner, now + 180, rid))
        row, record = self.check_record(rid)
        try:
            plan, poster, _ = self.validate(self.parent.verify(row[1], record["plan"]["report_commit"]), row[1], record["plan"]["report_commit"])
            if plan != record["plan"] or poster != row[2]:
                raise ValueError("revision_publication_mismatch")
        except Exception:
            self.defer(rid, "publication_read_failed")
            return self.status(rid)
        pieces = relay.chunks(record["text"])
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute("SELECT phase,owner,lease_until FROM scheduled_revision_notices WHERE request_id=?", (rid,)).fetchone()
            if owner[:2] != ("verifying", self.parent.owner):
                return self.status(rid)
            if not now <= self.parent.clock() < min(now + 60, owner[2]):
                self.defer_locked(rid, "verification_expired"); return self.status(rid)
            if self.parent.clock() < float(self.parent.store.get("next_send", "0")):
                self.db.execute("UPDATE scheduled_revision_notices SET phase='pending',next_at=? WHERE request_id=?", (float(self.parent.store.get("next_send")), rid))
                return self.status(rid)
            self.check_record(rid)
            parts, images = self.parts(rid), self.images(rid)
            if any(p[2] not in {"pending", "failed", "sent"} for p in parts + images):
                raise ValueError("revision_unreconciled_parts")
            part = next((p[0] for p in parts if p[2] != "sent"), None)
            if part is None and (not images or images[0][2] == "sent"):
                raise ValueError("revision_completion_ledger_differs")
            if part is None:
                self.db.execute("UPDATE image_parts SET status='sending' WHERE request_id=? AND file_id='scheduled-poster'", (rid,))
            else:
                self.db.execute("UPDATE parts SET status='sending' WHERE request_id=? AND part=?", (rid, part))
            self.db.execute("UPDATE scheduled_revision_notices SET phase='sending' WHERE request_id=?", (rid,))
            self.db.execute("UPDATE requests SET status='sending' WHERE id=?", (rid,))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('next_send',?)", (str(self.parent.clock() + 3.2),))
        try:
            outcome = self.parent.wecom.send(pieces[part]) if part is not None else self.parent.wecom.send_image(row[2], record["plan"]["mime"])
        except Exception:
            outcome = "unknown"
        if outcome not in {"sent", "failed", "unknown"}:
            outcome = "unknown"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if part is None:
                self.db.execute("UPDATE image_parts SET status=? WHERE request_id=? AND file_id='scheduled-poster'", (outcome, rid))
            else:
                self.db.execute("UPDATE parts SET status=? WHERE request_id=? AND part=?", (outcome, rid, part))
            if outcome == "sent":
                self.db.execute("UPDATE requests SET parts_sent=parts_sent+1 WHERE id=?", (rid,))
            complete = all(p[2] == "sent" for p in self.parts(rid) + self.images(rid))
            phase = "complete" if complete else "unknown" if outcome == "unknown" else "pending"
            self.db.execute("UPDATE scheduled_revision_notices SET phase=?,next_at=? WHERE request_id=?", (phase, self.parent.clock() + 3.2, rid))
            self.db.execute("UPDATE requests SET status=? WHERE id=?", ("sent" if complete else "queued" if outcome == "sent" else outcome, rid))
            if outcome == "failed":
                self.defer_locked(rid, "wecom_explicit_failure", phase="pending")
        return self.status(rid)

    def defer(self, rid, reason):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE"); self.defer_locked(rid, reason)

    def defer_locked(self, rid, reason, phase="verifying"):
        row = self.db.execute("SELECT attempts FROM scheduled_revision_notices WHERE request_id=? AND phase=? AND owner=?", (rid, phase, self.parent.owner)).fetchone()
        if row:
            attempts = row[0] + 1
            self.db.execute("UPDATE scheduled_revision_notices SET phase='pending',attempts=?,next_at=?,last_error=? WHERE request_id=?",
                            (attempts, self.parent.clock() + min(3600, 15 * 2 ** min(attempts - 1, 8)), reason, rid))
