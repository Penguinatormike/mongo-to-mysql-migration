import time
from dataclasses import replace

import pytest

from conftest import make_account


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_round_trip_preserves_fingerprint(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account(settings={"nested": {"z": 1, "a": [1, 2]}, "flag": False})
    store.create(a)
    assert store.get(a.id).fingerprint() == a.fingerprint()


def test_same_record_fingerprints_equal_across_stores(stores):
    mongo, mysql, _ = stores
    a = make_account()
    mongo.create(a)
    mysql.create(a)
    assert mongo.get(a.id).fingerprint() == mysql.get(a.id).fingerprint()


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_upsert_inserts_or_overwrites(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account()
    store.upsert(a)
    assert store.get(a.id) == a

    changed = replace(a, display_name="changed")
    store.upsert_many([changed])
    assert store.get(a.id) == changed and store.count() == 1


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_update_applies_changes_and_touches_updated_at(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account()
    store.create(a)
    updated = store.update(a.id, {"display_name": "Grace"})
    assert updated.display_name == "Grace"
    assert updated.updated_at > a.updated_at
    assert store.get(a.id) == updated
    assert store.update(make_account().id, {"plan": "free"}) is None


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_iter_batches_walks_everything_in_order(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    ids = sorted(make_account().id for _ in range(25))
    for id in ids:
        store.create(make_account(id=id))
    seen = [a.id for batch in store.iter_batches(batch_size=10) for a in batch]
    assert seen == ids
    assert [a.id for batch in store.iter_batches(batch_size=10, after_id=ids[9]) for a in batch] == ids[10:]


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_delete_removes_the_record(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account()
    store.create(a)
    assert store.delete(a.id) is True
    assert store.get(a.id) is None and store.count() == 0
    assert store.delete(a.id) is False  # already gone


# --- timeouts --------------------------------------------------------------------


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_store_with_timeout_fails_fast_against_a_hung_server(hung_server, which):
    from pymongo.errors import PyMongoError
    from sqlalchemy.exc import OperationalError

    from migrator.stores import MongoStore, MySQLStore

    if which == "mongo":
        store = MongoStore(url=f"mongodb://127.0.0.1:{hung_server}/?directConnection=true", db="x", timeout=0.3)
        timeout_error = PyMongoError
    else:
        store = MySQLStore(url=f"mysql+pymysql://app:app@127.0.0.1:{hung_server}/x", timeout=0.3)
        timeout_error = OperationalError
    started = time.monotonic()
    with pytest.raises(timeout_error):
        store.upsert(make_account())
    assert time.monotonic() - started < 3
