"""校验随包附带的本地 schema，绝不解析远程 schema 引用。"""

from datetime import datetime
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .storage import BriefError, read_json

# pip 会将 wheel 资源解包到包目录；运行时不依赖源码目录或当前工作目录。
RESOURCE_ROOT = Path(str(files("ai_brief").joinpath("resources")))


def validate(name, data, project=RESOURCE_ROOT):
    schema = read_json(Path(project) / "schemas" / f"{name}.schema.json")
    # 内置 schema 必须自包含，防止校验过程触发远程引用读取。
    def reject_refs(node):
        if isinstance(node, dict):
            if "$ref" in node or "$dynamicRef" in node:
                raise BriefError("External or local schema references are not supported")
            for value in node.values():
                reject_refs(value)
        elif isinstance(node, list):
            for value in node:
                reject_refs(value)
    reject_refs(schema)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data), key=lambda e: str(list(e.path)))
    if errors:
        error = errors[0]
        raise BriefError(f"{name} schema: {list(error.path)}: {error.message}")
    for field in ("as_of", "created_at", "updated_at"):
        if field in data:
            parse_time(data[field])
    for item in data.get("items", []):
        for field in ("event_key", "title", "source_name", "evidence", "summary", "relevance"):
            if not item[field].strip():
                raise BriefError(f"{field} must contain non-whitespace text")
        if any(character.isspace() or ord(character) < 32 for character in item["source_url"]):
            raise BriefError("Source URL cannot contain whitespace/control characters")
        try:
            parsed = urlsplit(item["source_url"])
            parsed.port  # 同时校验非法端口语法。
        except ValueError as exc:
            raise BriefError(f"Invalid source URL: {exc}") from exc
        if not parsed.hostname or parsed.username or parsed.password:
            raise BriefError("Source URL needs a hostname and must not contain credentials")
        if parse_time(item["retrieved_at"]) < parse_time(item["published_at"]):
            raise BriefError("retrieved_at must not precede published_at")


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone is required")
        return result
    except (ValueError, TypeError, AttributeError) as exc:
        raise BriefError(f"Invalid timezone-aware timestamp: {value!r}") from exc
