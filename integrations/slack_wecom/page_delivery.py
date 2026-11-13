"""不可变页面分区与原账本接续，不执行网络操作。"""
import re

TOPICS = ("model-serving-gpu", "hardware-status", "model-architecture-status", "embodied-training", "small-model-status", "inference-engines")

LABELS = ("模型服务与 GPU 超配", "硬件与算力平台", "模型与架构", "制造业具身智能训练", "前沿小模型", "推理引擎每日变动")


def require(ok):
    if not ok:
        raise ValueError("invalid_page_delivery")


def single_line(s):
    return isinstance(s, str) and s == s.strip() and bool(s) and len(s.encode()) <= 300 and not re.search(r"[\x00-\x1f\x7f-\x9f<>\u2028\u2029\ufeff\ud800-\udfff]", s)


def validate_pages(value, articles, day, revision):
    require(isinstance(value, dict) and set(value) == {"previous", "added", "pending"})
    require(isinstance(articles, list) and bool(articles) and all(isinstance(a, dict) and a.get("id") in TOPICS for a in articles))
    ready = [a["id"] for a in articles]
    require(len(set(ready)) == len(ready))
    pending, added = value["pending"], value["added"]
    require(isinstance(pending, list) and all(isinstance(p, dict) and set(p) == {"id", "reason"} and p["id"] in TOPICS and single_line(p["reason"]) for p in pending))
    all_ids = ready + [p["id"] for p in pending]
    require(len(all_ids) == 6 and len(set(all_ids)) == 6)
    require(isinstance(added, list) and (revision > 1 or bool(added)) and all(isinstance(i, str) and i in ready for i in added) and len(set(added)) == len(added))
    require(ready == [i for i in TOPICS if i in ready] and added == [i for i in ready if i in added] and [p["id"] for p in pending] == [i for i in TOPICS if i not in ready])
    prior = value["previous"]
    if revision == 1:
        require(prior is None and added == ready)
    else:
        require(isinstance(prior, dict) and set(prior) == {"request_id", "bundle_sha256"} and prior["request_id"] == f"morning-{day.replace('-', '')}-r{revision - 1}" and isinstance(prior["bundle_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", prior["bundle_sha256"]) is not None)
    return value


def notice_text(day, articles, pages):
    text = f"AI Infra 晨报 · {day}" + (" · 补充" if pages["previous"] else "") + "\n\n"
    text += "\n\n".join(f"{a['title']}\n{a['url']}" for a in articles if a["id"] in pages["added"])
    if pages["pending"]:
        text += "\n\n晨报说明：" + "；".join(f"{LABELS[TOPICS.index(p['id'])]}：{p['reason']}" for p in pages["pending"]) + "。下一次定时任务（下一小时 :20）继续补齐。"
    require(len(text.encode()) <= 1900)
    return text


def validate_state(state, day, revision):
    require(isinstance(state, dict) and set(state) == {"previous", "added", "pending", "articles"})
    validate_pages({k: state[k] for k in ("previous", "added", "pending")}, state["articles"], day, revision)
    require(all(set(a) == {"id", "title", "sha256"} and single_line(a["title"]) and isinstance(a["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", a["sha256"]) for a in state["articles"]))
    return state


def validate_progress(previous_plan, plan):
    old, new = previous_plan.get("page_state"), plan.get("page_state")
    require(old is not None and new is not None and previous_plan["report_date"] == plan["report_date"])
    require(new["previous"] == {"request_id": previous_plan["report_request_id"], "bundle_sha256": previous_plan["bundle_sha256"]})
    before = {a["id"]: a for a in old["articles"]}
    after = {a["id"]: a for a in new["articles"]}
    require(all(after.get(i) == a for i, a in before.items()))
    require(set(new["added"]) == set(after) - set(before))
    require(set(new["added"]) <= {p["id"] for p in old["pending"]})
    require({p["id"] for p in new["pending"]} == {p["id"] for p in old["pending"]} - set(new["added"]))


def validate_correction(previous_plan, plan):
    old, new = previous_plan.get("page_state"), plan.get("page_state")
    require(old is not None and new is not None and previous_plan["report_date"] == plan["report_date"])
    require(new["previous"] == {"request_id": previous_plan["report_request_id"], "bundle_sha256": previous_plan["bundle_sha256"]})
    require(new["added"] == [] and new["pending"] == old["pending"])
    require([a["id"] for a in new["articles"]] == [a["id"] for a in old["articles"]])
