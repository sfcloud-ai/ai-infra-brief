"""版本绑定的修订策略。分类随正文审查，不能由文本哈希变化自动推定。"""
import re


def validate_notice(value):
    if (not isinstance(value, dict) or set(value) != {"revision", "kind", "summary"}
            or type(value["revision"]) is not int or not 1 < value["revision"] <= 9007199254740991
            or value["kind"] not in {"correction", "material_update", "maintenance"}
            or not isinstance(value["summary"], str) or not value["summary"]
            or value["summary"].strip() != value["summary"] or len(value["summary"].encode()) > 600
            or re.search(r"[\x00-\x1f\x7f-\x9f<>\u2028\u2029\ufeff\ud800-\udfff]", value["summary"])):
        raise ValueError("invalid_revision_notice")
    return value


def update_text(day, notice, base):
    validate_notice(notice)
    label = "重要更正" if notice["kind"] == "correction" else "重要更新"
    return f"AI Infra 晨报 · {day} · {label}\n\n{notice['summary']}\n\n{base}{day}/"
