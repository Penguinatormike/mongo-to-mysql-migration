"""MySQL store. Shares its interface with MongoStore, so the migration code can treat either as source or target."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Iterable, Iterator

from sqlalchemy import bindparam, create_engine, text

from .. import config
from ..models import Account, utcnow

_COLUMNS = "id, email, display_name, plan, settings, created_at, updated_at"
_VALUES = ":id, :email, :display_name, :plan, :settings, :created_at, :updated_at"

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id           CHAR(24)     NOT NULL PRIMARY KEY,
        email        VARCHAR(255) NOT NULL,
        display_name VARCHAR(255) NOT NULL,
        plan         VARCHAR(32)  NOT NULL,
        settings     JSON         NOT NULL,
        created_at   DATETIME(3)  NOT NULL,
        updated_at   DATETIME(3)  NOT NULL
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

# Insert the row, or overwrite it if it already exists. Last writer wins.
_UPSERT = text(
    f"""
    INSERT INTO accounts ({_COLUMNS}) VALUES ({_VALUES}) AS new
    ON DUPLICATE KEY UPDATE
        email = new.email, display_name = new.display_name, plan = new.plan, settings = new.settings,
        created_at = new.created_at, updated_at = new.updated_at
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
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # --- application operations -------------------------------------------------

    def get(self, id: str) -> Account | None:
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id = :id")
        with self.engine.connect() as conn:
            row = conn.execute(stmt, {"id": id}).mappings().first()
        return self.from_row(row) if row else None

    def create(self, a: Account) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(f"INSERT INTO accounts ({_COLUMNS}) VALUES ({_VALUES})"), self.params(a))

    def update(self, id: str, changes: dict) -> Account | None:
        with self.engine.begin() as conn:
            row = conn.execute(text(f"SELECT {_COLUMNS} FROM accounts WHERE id = :id FOR UPDATE"), {"id": id})
            row = row.mappings().first()
            if row is None:
                return None
            updated = replace(self.from_row(row), **changes, updated_at=utcnow())
            conn.execute(
                text(
                    "UPDATE accounts SET email = :email, display_name = :display_name, plan = :plan,"
                    " settings = :settings, updated_at = :updated_at WHERE id = :id"
                ),
                self.params(updated),
            )
            return updated

    def delete(self, id: str) -> bool:
        """Returns True if there was a record to delete."""
        with self.engine.begin() as conn:
            return conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": id}).rowcount == 1

    # --- migration operations ---------------------------------------------------

    def upsert(self, a: Account) -> None:
        """Write this exact copy, inserting or overwriting. Last writer wins."""
        with self.engine.begin() as conn:
            conn.execute(_UPSERT, self.params(a))

    def upsert_many(self, accounts: Iterable[Account]) -> None:
        rows = [self.params(a) for a in accounts]
        if rows:
            with self.engine.begin() as conn:
                conn.execute(_UPSERT, rows)

    def get_many(self, ids: list[str]) -> dict[str, Account]:
        if not ids:
            return {}
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id IN :ids").bindparams(bindparam("ids", expanding=True))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": list(ids)}).mappings().all()
        return {r["id"]: self.from_row(r) for r in rows}

    def iter_batches(self, batch_size: int = 1000, after_id: str | None = None) -> Iterator[list[Account]]:
        """Walk every record in id order. Keyset pagination keeps every batch cheap and makes the walk resumable."""
        after = after_id or ""
        stmt = text(f"SELECT {_COLUMNS} FROM accounts WHERE id > :after ORDER BY id LIMIT :n")
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
            return int(conn.execute(text("SELECT COUNT(*) FROM accounts")).scalar_one())

    def reset(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM accounts"))
