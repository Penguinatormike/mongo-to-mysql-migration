"""Integration fixtures. Tests use a separate `testdb` database in both MongoDB and MySQL."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

import pytest
from bson import ObjectId

from migrator import config
from migrator.migration.flags import FixedSettings, Flags, StateStore
from migrator.migration.repository import AccountRepository
from migrator.models import Account, utcnow
from migrator.stores import MongoStore, MySQLStore

TEST_DB = "testdb"

ACCOUNT_FIELDS = {"email": "lin@example.com", "display_name": "Lin", "plan": "free", "settings": {}}

# The flag combinations the runbook steps through, in order (see demo/run.py). Nothing in the app
# requires these exact combinations; they're just the usual ones.
BEFORE = Flags()
DUAL_WRITE = Flags(write_mysql=True)
READ_MYSQL = Flags(write_mysql=True, read_mysql=True)
MYSQL_PRIMARY = Flags(write_mysql=True, read_mysql=True, mysql_primary=True)
CUT_OVER = Flags(write_mysql=True, read_mysql=True, mysql_primary=True, write_mongo=False)
RUNBOOK = [BEFORE, DUAL_WRITE, READ_MYSQL, MYSQL_PRIMARY, CUT_OVER]


def make_account(**overrides) -> Account:
    ts = utcnow() - timedelta(days=1)
    values = dict(
        id=str(ObjectId()),
        email="ada@example.com",
        display_name="Ada",
        plan="pro",
        settings={"theme": "dark", "notifications": True},
        created_at=ts,
        updated_at=ts,
    )
    values.update(overrides)
    return Account(**values)


def make_repo(stores, flags: Flags, fault_rate=0.0) -> AccountRepository:
    """A repository pinned to `flags`, with fault injection enabled so `fault_rate` takes effect."""
    mongo, mysql, _ = stores
    return AccountRepository(mongo, mysql, FixedSettings(flags, fault_rate), fault_injection=True)


def apply_flags(state: StateStore, flags: Flags) -> None:
    """Write every flag in `flags` to the shared state, as an operator would one by one."""
    for name, on in asdict(flags).items():
        state.set_flag(name, on)


@pytest.fixture(scope="session")
def _mongo():
    store = MongoStore(db=TEST_DB)
    yield store
    store.client.drop_database(TEST_DB)


@pytest.fixture(scope="session")
def _mysql():
    store = MySQLStore(url=config.MYSQL_URL.rsplit("/", 1)[0] + f"/{TEST_DB}")
    store.create_schema()
    yield store
    store.engine.dispose()


@pytest.fixture
def stores(_mongo, _mysql):
    """Empty Mongo + MySQL stores and migration state for each test."""
    state = StateStore(_mysql.engine)
    _mongo.reset()
    _mysql.reset()
    state.clear()
    return _mongo, _mysql, state


@pytest.fixture(scope="session")
def hung_server():
    """A TCP port that accepts connections but never answers, like a database that has hung.
    Returns the port."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    yield sock.getsockname()[1]
    sock.close()
