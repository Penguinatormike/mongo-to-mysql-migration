"""The reconciler: finding each kind of drift, ignoring in-flight writes, repairing until clean."""

from dataclasses import replace

from conftest import make_account
from migrator.migration.reconcile import reconcile, reconcile_until_clean


def test_reconcile_finds_and_repairs_every_kind_of_drift(stores):
    mongo, mysql, _ = stores
    ok, missing, changed = make_account(), make_account(), make_account()
    extra = make_account()
    for a in (ok, missing, changed):
        mongo.create(a)
    mysql.create(ok)
    mysql.create(replace(changed, display_name="out of date"))
    mysql.create(extra)  # deleted in Mongo, resurrected in MySQL

    report = reconcile(mongo, mysql, settle=0)
    assert (report.missing, report.mismatched, report.extra) == ([missing.id], [changed.id], [extra.id])

    reconcile(mongo, mysql, repair=True, settle=0)
    assert reconcile(mongo, mysql, settle=0).clean
    assert mysql.get(changed.id).display_name == changed.display_name
    assert mysql.get(extra.id) is None


class _LaggingTarget:
    """Wraps a store so its first get_many misses everything, like a shadow write that lands
    just after the reconciler reads the target."""

    def __init__(self, store):
        self.store = store
        self.lagged = False

    def get_many(self, ids):
        if not self.lagged:
            self.lagged = True
            return {}
        return self.store.get_many(ids)

    def __getattr__(self, name):
        return getattr(self.store, name)


def test_reconcile_counts_in_flight_writes_as_transient_not_drift(stores):
    mongo, mysql, _ = stores
    a = make_account()
    mongo.create(a)
    mysql.create(a)

    report = reconcile(mongo, _LaggingTarget(mysql), settle=0.01)
    assert report.clean
    assert report.transient == 1
    assert "transient=1" in report.summary()


def test_reconcile_until_clean_repairs_then_verifies(stores):
    mongo, mysql, _ = stores
    a, b = make_account(), make_account()
    mongo.create(a)
    mongo.create(b)
    mysql.create(replace(b, display_name="stale"))
    lines = []

    report = reconcile_until_clean(mongo, mysql, log=lines.append)
    assert report.clean
    assert len(lines) == 2  # one repair pass, one clean verification pass
    assert "missing=1" in lines[0] and "mismatched=1" in lines[0]
    assert mysql.get_many([a.id, b.id]) == {a.id: a, b.id: b}


def test_reconcile_until_clean_gives_up_after_max_passes(stores):
    mongo, mysql, _ = stores

    class _BrokenTarget(_LaggingTarget):
        def force_put(self, account):
            pass  # repairs never stick

    mongo.create(make_account())
    report = reconcile_until_clean(mongo, _BrokenTarget(mysql), max_passes=2)
    assert not report.clean


def test_reconcile_in_reverse_direction(stores):
    mongo, mysql, _ = stores
    a = make_account()
    mysql.create(a)
    report = reconcile(mongo, mysql, source="mysql", repair=True, settle=0)
    assert report.missing == [a.id]
    assert mongo.get(a.id) == a
