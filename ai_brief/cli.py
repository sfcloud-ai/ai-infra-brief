import argparse
from datetime import datetime
from pathlib import Path
import sys

from .contracts import RESOURCE_ROOT
from .pipeline import TZ, run, simulate_delivery
from .storage import BriefError, json_text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local-only AI brief prototype; no real messages, model calls, or schedules")
    parser.add_argument("--state-dir", type=Path, default=Path("var"), help="Local state directory (default: ./var)")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("run", help="Build a mock dry-run report once for the Asia/Shanghai day")
    generate.add_argument("--fixture", type=Path, default=RESOURCE_ROOT / "fixtures" / "empty.json")
    generate.add_argument("--history-dir", type=Path, help="Explicit prior brief JSON/dated Markdown directory")
    generate.add_argument("--as-of", help="Explicit ISO 8601 timestamp with UTC offset")
    generate.add_argument("--date", help="Must match --as-of in Asia/Shanghai; never backfills a range")
    generate.add_argument("--allow-backfill", action="store_true", help="Explicit single-date mock replay only")
    generate.add_argument("--adapter", default="mock", help="Only mock is connected; live fails closed")
    deliver = commands.add_parser("simulate-delivery", help="Write one LOCAL mock receipt, never an external message")
    deliver.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    deliver.add_argument("--adapter", default="mock")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run(args.state_dir, args.fixture, as_of=args.as_of, requested_date=args.date,
                         allow_backfill=args.allow_backfill, adapter=args.adapter, history_dir=args.history_dir)
        else:
            result = simulate_delivery(args.state_dir, args.date, adapter=args.adapter)
        print(json_text(result), end="")
        return 0
    except (BriefError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
