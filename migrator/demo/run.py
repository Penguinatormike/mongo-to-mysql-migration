"""End-to-end run of the whole migration under live traffic.

    seed Mongo -> write_mysql on -> backfill -> reconcile -> read_mysql on -> mysql_primary on -> write_mongo off

Each step flips one feature flag, the way the real migration was run.

A load generator runs the entire time and checks every response. The run passes only if no request
failed, no client ever saw inconsistent data, and the final reconcile finds no drift.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .. import config
from ..migration.backfill import run_backfill
from ..migration.flags import StateStore
from ..migration.reconcile import reconcile, reconcile_until_clean
from ..stores import MongoStore, MySQLStore
from .loadgen import LoadStats, run_load
from .seed import reset, seed

SETTLE = 3.0  # seconds of traffic after each flag flip (the app picks up a flip within 0.5s)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def banner(step: str) -> None:
    say(f"\n== {step} " + "=" * max(0, 70 - len(step)))


async def wait_for_app(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(base_url=url, timeout=2) as client:
        while True:
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"app at {url} never became healthy")
            await asyncio.sleep(0.5)


async def run_demo(count: int = 20_000, workers: int = 8, fault_rate: float = 0.05) -> int:
    mongo, mysql = MongoStore(), MySQLStore()
    mysql.create_schema()
    state = StateStore(mysql.engine)
    stats = LoadStats()
    stop = asyncio.Event()

    async def flip(flag: str, on: bool, note: str, settle: float = SETTLE) -> None:
        state.set_flag(flag, on)
        await asyncio.sleep(0.6)  # let the app's settings cache roll over before labelling traffic
        stats.label = f"{flag} {'on' if on else 'off'}"
        say(f"  flag {flag} -> {'on' if on else 'off'}: {note}")
        await asyncio.sleep(settle)

    banner("1. Seed the legacy MongoDB collection")
    reset(mongo, mysql, state)
    started = time.monotonic()
    seed(mongo, count)
    say(f"  {count:,} accounts in MongoDB, 0 in MySQL ({time.monotonic() - started:.1f}s)")

    await wait_for_app(config.APP_URL)
    seed_ids = [a.id for a in next(mongo.iter_batches(500))]
    stats.label = "before"
    load = asyncio.create_task(run_load(config.APP_URL, stop, workers=workers, seed_ids=seed_ids, stats=stats))
    say(f"  load generator started: {workers} concurrent clients")
    await asyncio.sleep(SETTLE)

    banner("2. Dual writes")
    state.set_fault_rate(fault_rate)  # demo scaffolding: gives the reconciler real drift to find
    await flip("write_mysql", True, "every write also goes to MySQL")
    if fault_rate:
        say(f"  (demo-only fault injection: {fault_rate:.0%} of those MySQL writes are made to fail on purpose)")

    banner("3. Backfill existing documents")
    result = await asyncio.to_thread(run_backfill, mongo, mysql, state, batch_size=500, pause=0.02, log=say)
    say(f"  copied {result['copied']:,} documents in {result['batches']} batches ({result['seconds']}s)")
    await asyncio.sleep(SETTLE)

    banner("4. Reconcile (verify only)")
    report = await asyncio.to_thread(reconcile, mongo, mysql, "mongo", False)
    say(f"  {report.summary()}")
    say(f"  -> {report.drift} records drifted: injected shadow-write failures, plus any writes that raced the backfill")

    banner("5. Stop injecting faults, repair until clean")
    state.set_fault_rate(0)
    await asyncio.sleep(1)
    report = await asyncio.to_thread(reconcile_until_clean, mongo, mysql, "mongo", 5, say)
    if not report.clean:
        say("  !! MySQL still drifting from MongoDB; aborting before reads move")
        stop.set()
        await load
        return 1

    banner("6. Move reads to MySQL")
    await flip("read_mysql", True, "reads served by MySQL; writes still land in MongoDB first")

    banner("7. Make MySQL the source of truth")
    await flip("mysql_primary", True, "writes land in MySQL first; MongoDB still written so a rollback stays possible")
    report = await asyncio.to_thread(reconcile_until_clean, mongo, mysql, "mysql", 5, say)
    if not report.clean:
        say("  !! MongoDB still drifting from MySQL; aborting before cutover (rollback copy isn't trustworthy)")
        stop.set()
        await load
        return 1

    banner("8. Cut over")
    await flip("write_mongo", False, "MongoDB no longer written; ready to decommission")

    stop.set()
    await load
    async with httpx.AsyncClient(base_url=config.APP_URL) as client:
        metrics = (await client.get("/metrics")).json()

    banner("Result")
    say(f"  {'after':<20}{'requests':>10}{'errors':>9}{'inconsistent':>14}")
    for step, c in stats.by_step.items():
        say(f"  {step:<20}{c['requests']:>10,}{c['errors']:>9}{c['consistency_errors']:>14}")
    say(f"  {'total':<20}{stats.requests:>10,}{stats.errors:>9}{stats.consistency_errors:>14}")
    say(f"  latency p50={stats.percentile(50):.1f}ms p99={stats.percentile(99):.1f}ms")
    say(f"  shadow writes: {metrics.get('shadow_writes_ok', 0):,} ok, {metrics.get('shadow_writes_failed', 0):,} failed (demo fault injection)")
    say(f"  MySQL rows: {mysql.count():,}")
    for line in stats.samples[:10]:
        say(f"  sample problem: {line}")

    passed = stats.errors == 0 and stats.consistency_errors == 0 and report.clean
    say("\n  PASS: zero failed requests, zero inconsistent reads, no drift" if passed else "\n  FAIL")
    return 0 if passed else 1


def main(count: int, workers: int, fault_rate: float) -> int:
    return asyncio.run(run_demo(count, workers, fault_rate))
