"""MySQL store. Shares its interface with MongoStore, so the migration code can treat either as source or target.

Deletes are tombstones: the row is kept with `deleted = 1` and a bumped version, so the version guard
also stops a stale copy (a backfill batch, a late shadow update) from bringing it back.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Iterable, Iterator

from sqlalchemy import bindparam, create_engine, text

from .. import config
from ..models import Account, utcnow

_COLUMNS = "id, email, display_name, plan, settings, version, created_at, updated_at"
_VALUES = ":id, :email, :display_name, :plan, :settings, :version, :created_at, :updated_at"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id           CHAR(24)        NOT NULL PRIMARY KEY,
        email        VARCHAR(255)    NOT NULL,
        display_name VARCHAR(255)    NOT NULL,
        plan         VARCHAR(32)     NOT NULL,
        settings     JSON            NOT NULL,
        version      BIGINT UNSIGNED NOT NULL,
        created_at   DATETIME(3)     NOT NULL,
        updated_at   DATETIME(3)     NOT NULL,
        deleted      BOOLEAN         NOT NULL DEFAULT FALSE
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_state (
        k          VARCHAR(64)  NOT NULL PRIMARY KEY,
        v          VARCHAR(255) NOT NULL,
        updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
    ) ENGINE=InnoDB
    """,
]

# Upgrades for tables created by an older version of SCHEMA: (column, ALTER statement).
_UPGRADES = [
    ("deleted", "ALTER TABLE accounts ADD COLUMN deleted BOOLEAN NOT NULL DEFAULT FALSE"),
]

# Version-guarded upsert: an older copy (e.g. a backfill batch read before a live update) never
# overwrites a newer row or tombstone. `version` is assigned last because MySQL applies assignments
# left to right, and every IF() above it must compare against the old version.
_UPSERT_IF_NEWER = text(
    f"""
    INSERT INTO accounts ({_COLUMNS}) VALUES ({_VALUES}) AS new
    ON DUPLICATE KEY UPDATE
        email        = IF(new.version > accounts.version, new.email,        accounts.email),
        display_name = IF(new.version > accounts.version, new.display_name, accounts.display_name),
        plan         = IF(new.version > accounts.version, new.plan,         accounts.plan),
        settings     = IF(new.version > accounts.version, new.settings,     accounts.settings),
        created_at   = IF(new.version > accounts.version, new.created_at,   accounts.created_at),
        updated_at   = IF(new.version > accounts.version, new.updated_at,   accounts.updated_at),
        deleted      = IF(new.version > accounts.version, FALSE,            accounts.deleted),
        version      = GREATEST(accounts.version, new.version)
    """
)

# Mirror a delete. If the row isn't here yet, insert a placeholder tombstone so a shadow write that
# arrives later can't create it either.
_UPSERT_TOMBSTONE = text(
    f"""
    INSERT INTO accounts ({_COLUMNS}, deleted)
    VALUES (:id, '', '', '', '{{}}', :version, :now, :now, TRUE) AS new
    ON DUPLICATE KEY UPDATE
        updated_at = IF(new.version > accounts.version, new.updated_at, accounts.updated_at),
        deleted    = IF(new.version > accounts.version, TRUE,           accounts.deleted),
        version    = GREATEST(accounts.version, new.version)
    """
)

_UPSERT_FORCE = text(
    f"""
    INSERT INTO accounts ({_COLUMNS}) VALUES ({_VALUES}) AS new
    ON DUPLICATE KEY UPDATE
        email = new.email, display_name = new.display_name, plan = new.plan, settings = new.settings,
        version = new.version, created_at = new.created_at, updated_at = new.updated_at, deleted = FALSE
    """
)


class MySQLStore:
    name = "mysql"

    def __init__(self, url: str | None = None, timeout: float | None = None):
        """`timeout` (seconds) bounds connecting, each read/write, and waiting for a pooled connection.
        Use it for shadow writes, so a hung secondary fails fast instead of stalling the request."""
        opts = {}
        if timeout is not None:
            opts = {
                "pool_timeout": timeout,
                "connect_args": {"connect_timeout": timeout, "read_timeout": timeout, "write_timeout": timeout},
            }
        self.engine = create_engine(
            url or config.MYSQL_URL, pool_size=20, max_overflow=20, pool_pre_ping=True, pool_recycle=3600, **opts
        )

    def create_schema(self) -> None:
        with self.engine.begin() as conn:
            for ddl in SCHEMA:
                conn.execute(text(ddl))
            for column, ddl in _UPGRADES:
                exists = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.columns"
                        " WHERE table_schema = DATABASE() AND table_name = 'accounts' AND column_name = :c"
                    ),
                    {"c": column},
                ).scalar_one()
                if not exists:
                    conn.execute(text(ddl))

    @staticmethod
    def from_row(row) -> Account:
        settings = row["settings"]
        if isinstance(settings, (str, bytes)):
            settings = json.loads(settings)
        return Account(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            plan=row["plan"],
            settings=settings or {},
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def params(a: Account) -> dict:
        return {
            "id": a.id,
            "email": a.email,
            "display_name": a.display_name,
            "plan": a.plan,
            "settings": json.dumps(a.settings, sort_keys=True),
            "version": a.version,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # --- application operations -------------------------------------------------

    def get(self, id: str) -> Account | None:
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id = :id AND NOT deleted")
        with self.engine.connect() as conn:
            row = conn.execute(stmt, {"id": id}).mappings().first()
        return self.from_row(row) if row else None

    def create(self, a: Account) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO accounts ({_COLUMNS}) VALUES ({_VALUES})"), self.params(a))

    def update(self, id: str, changes: dict) -> Account | None:
        with self.engine.begin() as conn:
            row = (
                conn.execute(
                    text(f"SELECT {_COLUMNS} FROM accounts WHERE id = :id AND NOT deleted FOR UPDATE"), {"id": id}
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            current = self.from_row(row)
            updated = replace(current, **changes, version=current.version + 1, updated_at=utcnow())
            conn.execute(
                text(
                    "UPDATE accounts SET email = :email, display_name = :display_name, plan = :plan,"
                    " settings = :settings, version = :version, updated_at = :updated_at WHERE id = :id"
                ),
                self.params(updated),
            )
            return updated

    def delete(self, id: str) -> int | None:
        """Tombstone a live record. Returns the tombstone's version, or None if there was nothing to delete."""
        with self.engine.begin() as conn:
            changed = conn.execute(
                text(
                    "UPDATE accounts SET deleted = TRUE, version = version + 1, updated_at = :now"
                    " WHERE id = :id AND NOT deleted"
                ),
                {"id": id, "now": utcnow()},
            ).rowcount
            if changed != 1:
                return None
            return int(conn.execute(text("SELECT version FROM accounts WHERE id = :id"), {"id": id}).scalar_one())

    # --- migration operations ---------------------------------------------------

    def put_if_newer(self, a: Account) -> None:
        with self.engine.begin() as conn:
            conn.execute(_UPSERT_IF_NEWER, self.params(a))

    def put_if_newer_many(self, accounts: Iterable[Account]) -> None:
        rows = [self.params(a) for a in accounts]
        if rows:
            with self.engine.begin() as conn:
                conn.execute(_UPSERT_IF_NEWER, rows)

    def put_tombstone(self, id: str, version: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(_UPSERT_TOMBSTONE, {"id": id, "version": version, "now": utcnow()})

    def force_put(self, a: Account) -> None:
        with self.engine.begin() as conn:
            conn.execute(_UPSERT_FORCE, self.params(a))

    def get_many(self, ids: list[str]) -> dict[str, Account]:
        if not ids:
            return {}
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id IN :ids AND NOT deleted").bindparams(
            bindparam("ids", expanding=True)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": list(ids)}).mappings().all()
        return {r["id"]: self.from_row(r) for r in rows}

    def iter_batches(self, batch_size: int = 1000, after_id: str | None = None) -> Iterator[list[Account]]:
        """Walk live records in id order. Keyset pagination keeps every batch cheap and makes the walk resumable."""
        after = after_id or ""
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id > :after AND NOT deleted ORDER BY id LIMIT :n")
        while True:
            with self.engine.connect() as conn:
                rows = conn.execute(stmt, {"after": after, "n": batch_size}).mappings().all()
            if not rows:
                return
            batch = [self.from_row(r) for r in rows]
            yield batch
            after = batch[-1].id
            if len(rows) < batch_size:
                return

    def count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM accounts WHERE NOT deleted")).scalar_one())

    def reset(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM accounts"))
