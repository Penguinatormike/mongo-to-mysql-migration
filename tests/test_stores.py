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
def test_put_if_newer_never_overwrites_a_newer_copy(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    newer = make_account(version=5, display_name="new")
    store.force_put(newer)

    store.put_if_newer(replace(newer, version=4, display_name="stale"))
    store.put_if_newer(replace(newer, version=5, display_name="same version"))
    assert store.get(newer.id).display_name == "new"

    store.put_if_newer(replace(newer, version=6, display_name="newest"))
    got = store.get(newer.id)
    assert (got.version, got.display_name) == (6, "newest")


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_update_bumps_version(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account(version=3)
    store.create(a)
    updated = store.update(a.id, {"display_name": "Grace"})
    assert (updated.version, updated.display_name) == (4, "Grace")
    assert updated.updated_at > a.updated_at
    assert store.get(a.id) == updated


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


# --- tombstones ------------------------------------------------------------------


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_delete_leaves_a_tombstone_that_blocks_stale_copies(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account(version=2)
    store.create(a)

    assert store.delete(a.id) == 3
    assert store.get(a.id) is None and store.get_many([a.id]) == {} and store.count() == 0
    assert list(store.iter_batches()) == []
    assert store.update(a.id, {"plan": "free"}) is None
    assert store.delete(a.id) is None  # already gone

    store.put_if_newer(a)  # a backfill batch read before the delete
    store.put_if_newer_many([replace(a, version=3)])
    store.put_tombstone(a.id, 2)
    assert store.get(a.id) is None

    store.force_put(a)  # repair from the source of truth is the one thing that may revive it
    assert store.get(a.id) == a


@pytest.mark.parametrize("which", ["mongo", "mysql"])
def test_tombstone_that_arrives_first_still_blocks_older_writes(stores, which):
    mongo, mysql, _ = stores
    store = {"mongo": mongo, "mysql": mysql}[which]
    a = make_account(version=2)
    store.put_tombstone(a.id, 3)  # the delete's shadow write overtook the create's
    store.put_if_newer(a)
    assert store.get(a.id) is None and store.count() == 0

    newer = replace(a, version=4)
    store.put_if_newer(newer)
    assert store.get(a.id) == newer


def test_mysql_schema_upgrade_adds_the_deleted_column(stores):
    _, mysql, _ = stores
    from sqlalchemy import text

    with mysql.engine.begin() as conn:
        conn.execute(text("ALTER TABLE accounts DROP COLUMN deleted"))
    mysql.create_schema()
    a = make_account()
    mysql.create(a)
    assert mysql.delete(a.id) == 2 and mysql.get(a.id) is None


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
        store.put_if_newer(make_account())
    assert time.monotonic() - started < 3
