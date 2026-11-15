"""Flag routing and shadow writes: which store each flag combination reads and writes, and how shadow
failures are handled."""

import time

from conftest import (
    ACCOUNT_FIELDS,
    BEFORE,
    CUT_OVER,
    DUAL_WRITE,
    MYSQL_PRIMARY,
    READ_MYSQL,
    make_account,
    make_repo,
)
from migrator.migration.flags import FixedSettings, Flags
from migrator.migration.reconcile import reconcile
from migrator.migration.repository import AccountRepository
from migrator.stores import MySQLStore


def test_before_the_migration_mysql_is_never_touched(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, BEFORE)
    a = repo.create(ACCOUNT_FIELDS)
    assert mongo.get(a.id) is not None and mysql.get(a.id) is None


def test_dual_write_keeps_both_stores_identical(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, DUAL_WRITE)
    a = repo.create(ACCOUNT_FIELDS)
    b = repo.create({**ACCOUNT_FIELDS, "email": "b@example.com"})
    repo.update(a.id, {"plan": "pro"})
    repo.delete(b.id)
    assert mysql.get(a.id).plan == "pro" and mysql.get(b.id) is None
    assert reconcile(mongo, mysql, settle=0).clean


def test_delete_still_removes_a_stray_shadow_copy(stores):
    mongo, mysql, _ = stores
    stray = make_account()
    mysql.create(stray)
    assert not make_repo(stores, DUAL_WRITE).delete(stray.id)
    assert mysql.get(stray.id) is None


def test_fault_injection_is_off_unless_enabled(stores):
    mongo, mysql, _ = stores
    repo = AccountRepository(mongo, mysql, FixedSettings(DUAL_WRITE, fault_rate=1.0))
    assert repo.fault_injection is False  # ENABLE_FAULT_INJECTION isn't set in tests
    a = repo.create(ACCOUNT_FIELDS)
    assert mysql.get(a.id) == a


def test_hung_shadow_fails_fast_and_silently(stores, hung_server):
    mongo, mysql, _ = stores
    hung = MySQLStore(url=f"mysql+pymysql://app:app@127.0.0.1:{hung_server}/x", timeout=0.3)
    repo = AccountRepository(mongo, mysql, FixedSettings(DUAL_WRITE), fast_shadows={"mysql": hung})
    started = time.monotonic()
    a = repo.create(ACCOUNT_FIELDS)
    assert time.monotonic() - started < 3
    assert mongo.get(a.id) == a
    assert repo.counters()["shadow_writes_failed"] == 1


def test_shadow_that_serves_reads_keeps_the_normal_client(stores):
    mongo, mysql, _ = stores
    fast = {"mongo": object(), "mysql": object()}
    repo = AccountRepository(mongo, mysql, FixedSettings(), fast_shadows=fast)
    assert repo.shadow_store(DUAL_WRITE) is fast["mysql"]
    assert repo.shadow_store(READ_MYSQL) is mysql  # MySQL serves reads here: don't drop its writes
    assert repo.shadow_store(MYSQL_PRIMARY) is fast["mongo"]


def test_failed_shadow_write_does_not_fail_the_request(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, DUAL_WRITE, fault_rate=1.0)
    a = repo.create(ACCOUNT_FIELDS)
    assert mongo.get(a.id) is not None and mysql.get(a.id) is None
    assert repo.counters()["shadow_writes_failed"] == 1
    assert reconcile(mongo, mysql, settle=0).missing == [a.id]


def test_read_mysql_serves_reads_from_mysql(stores):
    mongo, mysql, _ = stores
    only_in_mysql = make_account()
    mysql.create(only_in_mysql)
    assert make_repo(stores, DUAL_WRITE).get(only_in_mysql.id) is None
    assert make_repo(stores, READ_MYSQL).get(only_in_mysql.id) == only_in_mysql


def test_mysql_primary_writes_mysql_and_shadows_mongo(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, MYSQL_PRIMARY)
    a = repo.create(ACCOUNT_FIELDS)
    updated = repo.update(a.id, {"display_name": "Lin Q"})
    assert mysql.get(a.id) == updated
    assert mongo.get(a.id) == updated  # rollback copy kept current


def test_after_cutover_mongo_is_left_alone(stores):
    mongo, mysql, _ = stores
    a = make_repo(stores, CUT_OVER).create(ACCOUNT_FIELDS)
    assert mysql.get(a.id) is not None and mongo.get(a.id) is None


# --- odd combinations: allowed, like a real flag service ------------------------------


def test_read_mysql_without_write_mysql_reads_whatever_mysql_has(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, Flags(read_mysql=True))
    a = repo.create(ACCOUNT_FIELDS)
    assert mongo.get(a.id) == a and mysql.get(a.id) is None
    assert repo.get(a.id) is None  # nothing is writing to MySQL, so the read misses


def test_the_primary_is_always_written_even_with_its_write_flag_off(stores):
    mongo, mysql, _ = stores
    repo = make_repo(stores, Flags(mysql_primary=True, write_mysql=False, write_mongo=False))
    a = repo.create(ACCOUNT_FIELDS)
    assert mysql.get(a.id) == a and mongo.get(a.id) is None
