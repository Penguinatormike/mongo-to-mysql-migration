"""Copy every existing Mongo document into MySQL while live traffic keeps writing to both."""

from __future__ import annotations

import time
from typing import Callable

from .. import config
from ..models import utcnow
from .flags import StateStore

CHECKPOINT_KEY = "backfill_last_id"


def wait_for_flag_rollout(
    state: StateStore, flag: str, propagation: float, log: Callable[[str], None] | None = None
) -> None:
    """Block until every app instance has picked up the last flip of `flag`. Instances cache flags for up to
    SETTINGS_TTL_SECONDS, and a request already in flight keeps the flags it started with."""
    age = state.age(flag)
    wait = propagation - (age if age is not None else 0.0)
    if wait > 0:
        if log:
            log(f"  waiting {wait:.1f}s for every app instance to pick up {flag}")
        time.sleep(wait)


def run_backfill(
    mongo,
    mysql,
    state: StateStore | None = None,
    batch_size: int = 500,
    pause: float = 0.0,
    resume: bool = True,
    log: Callable[[str], None] | None = None,
    propagation: float | None = None,
) -> dict:
    """Walk Mongo in _id order and upsert each batch into MySQL.

    Safe to run against live traffic because:
      * it only runs once every app instance is dual-writing, so anything written after a batch is read
        reaches MySQL anyway;
      * the upsert is version-guarded and deletes are versioned tombstones, so a batch read before a live
        update or delete never clobbers it or brings the record back;
      * progress is checkpointed, so a crashed or paused run picks up where it stopped. A finished run
        clears the checkpoint, so running it again re-copies everything (harmless, thanks to the guard).

    `propagation` is how long write_mysql must have been on before copying starts; it defaults to twice
    the settings cache TTL.
    """
    if state is not None:
        flags = state.flags()
        if not (flags.primary == "mongo" and flags.shadow == "mysql"):
            raise RuntimeError(
                "The backfill copies MongoDB into MySQL, so it needs write_mysql on and mysql_primary off, "
                f"or writes made during the backfill would be lost. Flags: {flags.describe()}"
            )
        propagation = 2 * config.SETTINGS_TTL_SECONDS if propagation is None else propagation
        wait_for_flag_rollout(state, "write_mysql", propagation, log)

    after = state.get(CHECKPOINT_KEY) if (state is not None and resume) else None
    copied = batches = 0
    started = time.monotonic()

    for batch in mongo.iter_batches(batch_size, after):
        mysql.put_if_newer_many(batch)
        copied += len(batch)
        batches += 1
        if state is not None:
            state.set(CHECKPOINT_KEY, batch[-1].id)
        if log and batches % 10 == 0:
            log(f"  backfilled {copied:,} documents")
        if pause:
            time.sleep(pause)  # throttle so the backfill doesn't starve production traffic

    if state is not None:
        state.delete(CHECKPOINT_KEY)
        state.set("backfill_completed_at", utcnow().isoformat())
    return {"copied": copied, "batches": batches, "seconds": round(time.monotonic() - started, 2)}
