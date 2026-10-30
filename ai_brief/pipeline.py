"""单次执行的本地流水线，可供未来的外部调度器调用。"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .adapters import MockDeliveryAdapter, MockEditorAdapter, MockResearchAdapter, require_mock
from .contracts import RESOURCE_ROOT, parse_time, validate
from .render import graphic, report
from .storage import BriefError, atomic_write, digest, json_text, lock, read_json, sync_directory, write_json

TZ = ZoneInfo("Asia/Shanghai")
TOPIC_ORDER = {"model_serving": 0, "manufacturing_embodied_training": 1, "other_ai_infra": 2}
ARTIFACTS = ("brief.json", "brief.md", "overview.svg", "inputs.json")


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    # 协议、片段、末尾斜线和常见追踪参数不构成新事件。
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def event_key(item):
    return " ".join(item["event_key"].casefold().split())


def config_at(project):
    config = read_json(project / "config.json")
    required = {"schema_version", "timezone", "lookback_hours", "minimum_items", "maximum_items", "history_days"}
    if set(config) != required or config["schema_version"] != "1.0" or config["timezone"] != "Asia/Shanghai" or config["lookback_hours"] != [24, 72, 168]:
        raise BriefError("Unsupported config: expected v1, Asia/Shanghai, and [24,72,168] windows")
    if any(type(config[k]) is not int for k in ("minimum_items", "maximum_items", "history_days")):
        raise BriefError("Config counts must be integers")
    if not 1 <= config["minimum_items"] <= config["maximum_items"] <= 8 or not 1 <= config["history_days"] <= 366:
        raise BriefError("Config bounds: 1 <= minimum <= maximum <= 8, history_days 1..366")
    return config


def load_history(root, day, days, project, history_dir=None):
    earliest = day - timedelta(days=days)
    # 内部日期由运行器生成；窗口外的过期状态不参与解析。
    controlled = {}
    for age in range(1, days + 1):
        previous = day - timedelta(days=age)
        directory = root / "runs" / previous.isoformat()
        if directory.exists():
            controlled[(directory / "brief.json").resolve()] = previous
    paths = list(controlled)
    if history_dir:
        extra = Path(history_dir)
        if not extra.is_dir():
            raise BriefError(f"History directory does not exist: {extra}")
        paths.extend(extra.rglob("*.json"))
        paths.extend(extra.rglob("*.md"))
    history, urls, events = [], set(), set()
    for path in sorted(set(p.resolve() for p in paths)):
        if path.stat().st_size > 1_000_000:
            raise BriefError(f"History file exceeds 1 MB: {path}")
        if path.suffix == ".json":
            data = read_json(path)
            validate("brief", data, project)
            previous_day = date.fromisoformat(data["date"])
            if path in controlled and controlled[path] != previous_day:
                raise BriefError(f"History content date does not match run directory: {path}")
            if not earliest <= previous_day < day:
                continue
            rows = data["items"]
            urls.update(canonical_url(x["source_url"]) for x in rows)
            events.update(event_key(x) for x in rows)
            content = data
        else:
            # 既有 Markdown 的文件名或父目录必须包含日期。
            matches = re.findall(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)", str(path))
            if not matches:
                raise BriefError(f"Legacy history must have YYYY-MM-DD or YYYYMMDD in its path: {path}")
            try:
                previous_day = date(*map(int, matches[-1]))
            except ValueError as exc:
                raise BriefError(f"Invalid history date in {path}") from exc
            if not earliest <= previous_day < day:
                continue
            content = path.read_text(encoding="utf-8")
            for url in re.findall(r"https?://[^\s<>\[\]\"']+", content):
                url = url.rstrip(".,;，。；")
                while url.endswith(")") and url.count(")") > url.count("("):
                    url = url[:-1]
                urls.add(canonical_url(url))
        history.append({"path": str(path), "date": previous_day.isoformat(), "sha256": digest(path.read_bytes()), "content": content})
    return history, urls, events


def select_items(items, as_of, hours, urls, events, maximum):
    counts = {"future": 0, "stale": 0, "duplicate_history": 0, "duplicate_current": 0, "trimmed": 0}
    selected, eligible = [], []
    for item in sorted(items, key=lambda x: (TOPIC_ORDER[x["topic"]], -parse_time(x["published_at"]).timestamp(), x["event_key"])):
        published = parse_time(item["published_at"])
        if published > as_of or parse_time(item["retrieved_at"]) > as_of:
            counts["future"] += 1
        elif published < as_of - timedelta(hours=hours):
            counts["stale"] += 1
        else:
            eligible.append(item)
    # 先合并 URL/事件的等价关系，避免 A~B~C 的传递重复留下两个代表条目。
    parents = list(range(len(eligible)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    identities = {}
    historical = set()
    for index, item in enumerate(eligible):
        url, event = canonical_url(item["source_url"]), event_key(item)
        for identity in (("url", url), ("event", event)):
            if identity in identities:
                parents[find(index)] = find(identities[identity])
            identities[identity] = index
        if url in urls or event in events:
            historical.add(index)
    historical_roots = {find(index) for index in historical}
    seen_roots = set()
    for index, item in enumerate(eligible):
        group = find(index)
        if group in historical_roots:
            counts["duplicate_history"] += 1
        elif group in seen_roots:
            counts["duplicate_current"] += 1
        else:
            seen_roots.add(group)
            selected.append(item)
    counts["trimmed"] = max(0, len(selected) - maximum)
    return selected[:maximum], counts


def implementation_fingerprint(project):
    # 使用逻辑路径，保证源码目录与安装后的 wheel 得到相同指纹。
    code = Path(__file__).resolve().parent
    hashes = {f"ai_brief/{p.name}": digest(p.read_bytes()) for p in sorted(code.glob("*.py"))}
    for path in sorted(Path(project).rglob("*")):
        if path.is_file():
            hashes[f"resources/{path.relative_to(project).as_posix()}"] = digest(path.read_bytes())
    return digest(json_text(hashes))


def verify_artifacts(run_dir, manifest, project):
    validate("manifest", manifest, project)
    for name, expected in manifest["artifacts"].items():
        path = run_dir / name
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise BriefError(f"Artifact missing or changed: {path}; refusing reuse/delivery")
    validate("brief", read_json(run_dir / "brief.json"), project)


def run(root, fixture, *, as_of=None, requested_date=None, allow_backfill=False, adapter="mock", project=RESOURCE_ROOT, history_dir=None):
    require_mock(adapter)
    project, root = Path(project).resolve(), Path(root).resolve()
    as_of = parse_time(as_of) if as_of else datetime.now(timezone.utc)
    day = as_of.astimezone(TZ).date()
    if requested_date and requested_date != day.isoformat():
        raise BriefError("--date must equal the Asia/Shanghai date of --as-of; backfill never loops over dates")
    if day != datetime.now(TZ).date() and not allow_backfill:
        raise BriefError("Historical/future replay requires explicit --allow-backfill and --as-of; no automatic backfill")
    config = config_at(project)
    prompts = {name: (project / "prompts" / name).read_text(encoding="utf-8") for name in ("research.md", "editor.md")}
    research = MockResearchAdapter(fixture, project)
    implementation = implementation_fingerprint(project)
    # 测试输入保存完整快照；模拟研究在不同时间窗口内复用同一份内容。
    with lock(root):
        run_dir = root / "runs" / day.isoformat()
        if run_dir.exists():
            manifest = read_json(run_dir / "manifest.json")
            verify_artifacts(run_dir, manifest, project)
            # 调度重试时 as_of 会随时钟前进；比较稳定输入，而不是再次触发时的时间。
            previous_inputs = read_json(run_dir / "inputs.json")
            stable = {"config": config, "fixture": research.data, "prompts": prompts, "implementation_sha256": implementation, "history_dir": str(Path(history_dir).resolve()) if history_dir else None}
            if previous_inputs["stable_inputs"] != stable:
                raise BriefError("This date already has a different input snapshot. Preserve it; use a separate --state-dir for an explicit new mock experiment")
            return {"reused": True, "run_dir": str(run_dir), "manifest": manifest}

        history, urls, events = load_history(root, day, config["history_days"], project, history_dir)
        context = {"date": day.isoformat(), "as_of": as_of.isoformat(), "history": history, "history_files": [h["path"] for h in history]}
        attempts = []
        for hours in config["lookback_hours"]:
            context["window_hours"] = hours
            candidates = research.research(context, prompts["research.md"])
            validate("candidates", candidates, project)
            selected, exclusions = select_items(candidates["items"], as_of, hours, urls, events, config["maximum_items"])
            attempts.append({"hours": hours, "qualified_items": len(selected)})
            if len(selected) >= config["minimum_items"]:
                break
        context["notes"] = list(candidates["notes"]) + [
            "实际 adapter：本地 fixture 研究 + 确定性编辑；两阶段 prompt 未被模型执行。",
            f"已读取 {len(history)} 份历史报告；URL/事件键去重已执行，语义去重尚未接入模型。",
            "未达到目标数量时保留不足或零项，不使用过期条目补位。",
        ]
        brief = MockEditorAdapter().edit(selected, context, prompts["editor.md"])
        validate("brief", brief, project)
        stable = {"config": config, "fixture": research.data, "prompts": prompts, "implementation_sha256": implementation, "history_dir": str(Path(history_dir).resolve()) if history_dir else None}
        inputs = {"stable_inputs": stable, "history": history, "research_context": context}
        outputs = {"brief.json": json_text(brief), "brief.md": report(brief), "overview.svg": graphic(brief), "inputs.json": json_text(inputs)}
        manifest = {
            "schema_version": "1.0", "date": day.isoformat(), "timezone": "Asia/Shanghai", "mode": "mock",
            "as_of": as_of.isoformat(), "created_at": now_utc(), "status": "dry_run_complete", "input_fingerprint": digest(json_text(inputs)),
            "implementation_sha256": implementation,
            "prompt_hashes": {name: digest(value) for name, value in prompts.items()}, "window_attempts": attempts,
            "exclusions": exclusions, "history_files": context["history_files"],
            "artifacts": {name: digest(value) for name, value in outputs.items()},
            "real_adapter_status": {"research": "not_connected", "editor": "not_connected", "delivery": "not_connected"},
            "delivery": {"real_status": "not_sent", "simulation_ledger": "delivery.simulation.json"},
        }
        validate("manifest", manifest, project)
        key = digest(f"ai-brief:v1:simulation:{day}:local-mock")
        ledger = {"schema_version": "1.0", "mode": "simulation", "key": key, "brief_sha256": manifest["artifacts"]["brief.json"],
                  "destination": "local-mock", "status": "prepared", "attempts": 0, "updated_at": now_utc(), "receipt": None, "last_error": None}
        validate("delivery", ledger, project)
        parent = root / "runs"
        parent.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{day}.staging-", dir=parent))
        try:
            for name, content in outputs.items():
                atomic_write(staging / name, content)
            write_json(staging / "delivery.simulation.json", ledger)
            write_json(staging / "manifest.json", manifest)
            staging.rename(run_dir)
            sync_directory(parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {"reused": False, "run_dir": str(run_dir), "manifest": manifest}


def simulate_delivery(root, day, *, adapter="mock", project=RESOURCE_ROOT, sender=None):
    require_mock(adapter)
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError()
    except ValueError as exc:
        raise BriefError("Delivery date must be YYYY-MM-DD") from exc
    root, project = Path(root).resolve(), Path(project).resolve()
    with lock(root):
        run_dir = root / "runs" / day
        manifest = read_json(run_dir / "manifest.json")
        verify_artifacts(run_dir, manifest, project)
        path = run_dir / "delivery.simulation.json"
        ledger = read_json(path)
        validate("delivery", ledger, project)
        expected_key = digest(f"ai-brief:v1:simulation:{day}:local-mock")
        if ledger["key"] != expected_key or ledger["brief_sha256"] != manifest["artifacts"]["brief.json"]:
            raise BriefError("Delivery ledger does not match date/payload")
        if ledger["status"] == "simulated":
            return {"reused": True, "ledger": ledger}
        if ledger["status"] in {"in_flight", "uncertain"}:
            raise BriefError("Previous delivery outcome is uncertain; refusing automatic resend. Reconcile the local receipt manually")
        ledger.update(status="in_flight", attempts=ledger["attempts"] + 1, updated_at=now_utc())
        write_json(path, ledger)  # 在可能产生副作用前，先持久化本次投递的占用状态。
        sender = sender or MockDeliveryAdapter(root / "mock-outbox")
        try:
            receipt = sender.send(ledger["key"], read_json(run_dir / "brief.json"))
            if not isinstance(receipt, str) or not receipt:
                raise BriefError("Adapter returned no verifiable receipt")
        except Exception as exc:
            ledger.update(status="uncertain", last_error=f"{type(exc).__name__}: {exc}", updated_at=now_utc())
            write_json(path, ledger)
            raise BriefError("Simulation send failed; outcome marked uncertain and automatic resend blocked") from exc
        ledger.update(status="simulated", receipt=receipt, updated_at=now_utc())
        write_json(path, ledger)
        return {"reused": False, "ledger": ledger}
