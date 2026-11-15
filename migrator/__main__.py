"""Command line: python -m migrator <command>  (in Docker: docker compose run --rm tools <command>)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import config
from .migration.flags import FLAG_NAMES, StateStore
from .stores import MongoStore, MySQLStore


def _stores():
    mongo, mysql = MongoStore(), MySQLStore()
    mysql.create_schema()
    return mongo, mysql, StateStore(mysql.engine)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrator", description="Demo tooling for the MongoDB -> MySQL migration. Not production code."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="run the whole migration under live load")
    p.add_argument("--count", type=int, default=20_000)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--fault-rate", type=float, default=0.05, help="demo-only: share of shadow writes made to fail on purpose")

    p = sub.add_parser("seed", help="reset both databases and seed MongoDB")
    p.add_argument("--count", type=int, default=20_000)

    sub.add_parser("status", help="show flags, counts and app metrics")

    p = sub.add_parser("flag", help="show the migration flags, or flip one: flag write_mysql on")
    p.add_argument("name", nargs="?", choices=FLAG_NAMES)
    p.add_argument("value", nargs="?", choices=["on", "off"])

    p = sub.add_parser("fault", help="set the injected shadow-write failure rate (0..1); the app only honours it with ENABLE_FAULT_INJECTION=1")
    p.add_argument("rate", type=float)

    p = sub.add_parser("backfill", help="copy MongoDB documents into MySQL")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--pause", type=float, default=0.0, help="seconds to sleep between batches")
    p.add_argument("--restart", action="store_true", help="ignore the checkpoint and start from the beginning")

    p = sub.add_parser("reconcile", help="compare the stores, optionally repairing the target")
    p.add_argument("--source", choices=["mongo", "mysql"], default="mongo")
    p.add_argument("--repair", action="store_true")

    p = sub.add_parser("loadgen", help="send checked traffic at the app")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--workers", type=int, default=8)

    args = parser.parse_args(argv)

    if args.command == "demo":
        from .demo.run import main as demo

        return demo(args.count, args.workers, args.fault_rate)

    mongo, mysql, state = _stores()

    if args.command == "seed":
        from .demo.seed import reset, seed

        reset(mongo, mysql, state)
        seed(mongo, args.count)
        print(f"seeded {args.count:,} accounts into MongoDB; flags reset to their defaults")

    elif args.command == "status":
        import httpx

        try:
            metrics = httpx.get(f"{config.APP_URL}/metrics", timeout=3).json()
        except httpx.HTTPError as exc:
            metrics = {"error": repr(exc)}
        print(json.dumps({"flags": state.flags().describe(), "state": state.all(), "mongo": mongo.count(),
                          "mysql": mysql.count(), "app": metrics}, indent=2))

    elif args.command == "flag":
        if args.name and args.value:
            state.set_flag(args.name, args.value == "on")
        elif args.name:
            parser.error("give a value: flag <name> on|off")
        print(state.flags().describe())

    elif args.command == "fault":
        state.set_fault_rate(args.rate)
        print(f"shadow fault rate = {args.rate}")

    elif args.command == "backfill":
        from .migration.backfill import run_backfill

        print(run_backfill(mongo, mysql, state, args.batch_size, args.pause, resume=not args.restart, log=print))

    elif args.command == "reconcile":
        from .migration.reconcile import reconcile

        report = reconcile(mongo, mysql, source=args.source, repair=args.repair)
        print(report.summary())
        for kind in ("missing", "mismatched", "extra"):
            ids = getattr(report, kind)
            if ids:
                print(f"  {kind}: {', '.join(ids[:10])}{' ...' if len(ids) > 10 else ''}")
        return 0 if report.clean or args.repair else 1

    elif args.command == "loadgen":
        from .demo.loadgen import run_load

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(run_load(config.APP_URL, stop, workers=args.workers))
            await asyncio.sleep(args.duration)
            stop.set()
            return await task

        stats = asyncio.run(go())
        print(f"requests={stats.requests:,} errors={stats.errors} inconsistent={stats.consistency_errors} "
              f"p50={stats.percentile(50):.1f}ms p99={stats.percentile(99):.1f}ms")
        return 0 if stats.errors == 0 and stats.consistency_errors == 0 else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
