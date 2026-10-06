"""Run a single, read-only discovery snapshot."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from .http import PublicClient
from .ingest import snapshot
from .sources import ADAPTERS


def bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return parse


def timeout_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(number) or not 0 < number <= 60:
        raise argparse.ArgumentTypeError("must be finite and between 0 and 60 seconds")
    return number


def write_snapshot(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".arb-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only prediction-market ingestion; trading is disabled.")
    parser.add_argument("--source", choices=tuple(ADAPTERS), action="append",
                        help="Select a source; repeat for both (default: both)")
    parser.add_argument("--offline", action="store_true", help="Use bundled synthetic fixtures; no network access")
    parser.add_argument("--limit", type=bounded_int(1, 10000), default=100, help="Maximum raw records per source (default: 100)")
    parser.add_argument("--page-size", type=bounded_int(1, 100), default=100, help="Live request page size (default: 100)")
    parser.add_argument("--max-pages", type=bounded_int(1, 100), default=5, help="Maximum pages per source (default: 5)")
    parser.add_argument("--timeout", type=timeout_seconds, default=10, help="Per-request timeout in seconds (default: 10)")
    parser.add_argument("--retries", type=bounded_int(0, 3), default=2, help="Retries after first attempt (default: 2)")
    parser.add_argument("--output", type=Path, help="Write snapshot atomically to this JSON file; otherwise stdout")
    args = parser.parse_args(argv)
    result = snapshot(args.source or list(ADAPTERS), limit=args.limit, page_size=args.page_size,
                      max_pages=args.max_pages, offline=args.offline,
                      client=None if args.offline else PublicClient(args.timeout, args.retries))
    content = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    try:
        if args.output:
            write_snapshot(args.output, content)
        else:
            sys.stdout.write(content)
    except OSError as exc:
        print(f"Could not write snapshot: {exc}", file=sys.stderr)
        return 1
    for source, report in result["sources"].items():
        message = (f"{source}: {report['status']}; {report['accepted']} accepted, "
                   f"{len(report['rejected'])} rejected; stop={report['stop_reason']}")
        if report["error"]:
            message += f"; {report['error']}"
        print(message, file=sys.stderr)
    return 0 if result["success"] else 1
