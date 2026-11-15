"""The backfill: flag guard, rollout wait, checkpointing, and staying safe under live writes."""

import time

import pytest

from conftest import BEFORE, CUT_OVER, DUAL_WRITE, MYSQL_PRIMARY, apply_flags, make_account, make_repo
from migrator.migration.backfill import CHECKPOINT_KEY, run_backfill
from migrator.migration.flags import Flags
from migrator.migration.reconcile import reconcile


def test_backfill_copies_everything(stores):
    mongo, mysql, _ = stores
    for _ in range(120):
        mongo.create(make_account())
    result = run_backfill(mongo, mysql, batch_size=50)
    assert result["copied"] == 120
    assert reconcile(mongo, mysql, settle=0).clean


def test_backfill_refuses_to_run_before_dual_writes(stores):
    mongo, mysql, state = stores
    with pytest.raises(RuntimeError, match="write_mysql"):
        run_backfill(mongo, mysql, state)


def test_backfill_resumes_from_checkpoint(stores):
    mongo, mysql, state = stores
    state.set_flag("write_mysql", True)
    accounts = sorted((make_account() for _ in range(30)), key=lambda a: a.id)
    for a in accounts:
        mongo.create(a)
    state.set(CHECKPOINT_KEY, accounts[19].id)  # pretend a previous run got this far

    assert run_backfill(mongo, mysql, state, batch_size=7, propagation=0)["copied"] == 10
    assert mysql.count() == 10
    assert state.get(CHECKPOINT_KEY) is None  # finished, so a re-run starts from the beginning
    assert run_backfill(mongo, mysql, state, batch_size=7, propagation=0)["copied"] == 30


@pytest.mark.parametrize(
    "flags", [BEFORE, MYSQL_PRIMARY, CUT_OVER, Flags(read_mysql=True)], ids=lambda f: f.describe()
)
def test_backfill_only_runs_while_mongo_is_primary_and_mysql_is_written(stores, flags):
    mongo, mysql, state = stores
    apply_flags(state, flags)
    with pytest.raises(RuntimeError, match="write_mysql"):
        run_backfill(mongo, mysql, state, propagation=0)


def test_backfill_waits_for_every_instance_to_see_dual_writes(stores):
    mongo, mysql, state = stores
    state.set_flag("write_mysql", True)
    lines = []
    started = time.monotonic()
    run_backfill(mongo, mysql, state, propagation=0.4, log=lines.append)
    assert time.monotonic() - started >= 0.3
    assert "waiting" in lines[0]

    lines.clear()
    run_backfill(mongo, mysql, state, propagation=0.4, log=lines.append)  # the flip is old by now
    assert lines == []


class _WriteAfterRead:
    """Wraps the source so live traffic changes records after the backfill has read their batch."""

    def __init__(self, store, on_read):
        self.store, self.on_read = store, on_read

    def iter_batches(self, *args, **kwargs):
        for batch in self.store.iter_batches(*args, **kwargs):
            self.on_read(batch)
            yield batch

    def __getattr__(self, name):
        return getattr(self.store, name)


def test_writes_that_race_the_backfill_are_drift_the_reconciler_repairs(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, DUAL_WRITE)
    for _ in range(5):
        mongo.create(make_account())
    updated, deleted = next(mongo.iter_batches())[:2]

    def live_traffic(batch):
        repo.update(updated.id, {"display_name": "changed live"})
        repo.delete(deleted.id)

    # The batch was read before those writes, so it copies the old record and brings back the deleted one.
    run_backfill(_WriteAfterRead(mongo, live_traffic), mysql)
    assert mysql.get(updated.id).display_name == "Ada"
    assert mysql.get(deleted.id) is not None

    report = reconcile(mongo, mysql, repair=True, settle=0)
    assert (report.mismatched, report.extra) == ([updated.id], [deleted.id])
    assert reconcile(mongo, mysql, settle=0).clean
