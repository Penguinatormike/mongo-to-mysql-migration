"""Feature flags that control the migration, and the shared runtime settings that read them.

The flags live in a small MySQL table, standing in for a feature-flag service (LaunchDarkly and the
like). Every app instance re-reads them every SETTINGS_TTL_SECONDS, so flipping a flag rolls out
without a deploy.

Each flag is flipped on its own and nothing checks that the combination makes sense, as with a real
flag service. Keeping the combination sane is the operator's job; the README has the usual order.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, fields, replace

from sqlalchemy import text

from .. import config


@dataclass(frozen=True)
class Flags:
    """The migration flags. Defaults are the state before the migration starts."""

    write_mysql: bool = False  # also write every change to MySQL
    read_mysql: bool = False  # serve reads from MySQL instead of MongoDB
    mysql_primary: bool = False  # MySQL is the source of truth: writes must succeed there first
    write_mongo: bool = True  # keep writing changes to MongoDB (turned off at the very end)

    @property
    def primary(self) -> str:
        """The database every write must succeed in. It is always written, whatever its write_* flag says."""
        return "mysql" if self.mysql_primary else "mongo"

    @property
    def shadow(self) -> str | None:
        """The other database, if its write flag is on. Writes to it are best effort."""
        other = "mongo" if self.mysql_primary else "mysql"
        return other if getattr(self, f"write_{other}") else None

    @property
    def reads(self) -> str:
        return "mysql" if self.read_mysql else "mongo"

    def describe(self) -> str:
        return " ".join(f"{name}={'on' if value else 'off'}" for name, value in asdict(self).items())


FLAG_NAMES = [f.name for f in fields(Flags)]


def _parse(value: str | None, default: bool) -> bool:
    return default if value is None else value.lower() in ("1", "on", "true", "yes")


class StateStore:
    def __init__(self, engine):
        self.engine = engine

    def get(self, key: str, default: str | None = None) -> str | None:
        with self.engine.connect() as conn:
            value = conn.execute(text("SELECT v FROM migration_state WHERE k = :k"), {"k": key}).scalar()
        return default if value is None else value

    def all(self) -> dict[str, str]:
        with self.engine.connect() as conn:
            return {k: v for k, v in conn.execute(text("SELECT k, v FROM migration_state"))}

    def set(self, key: str, value: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO migration_state (k, v) VALUES (:k, :v) AS new ON DUPLICATE KEY UPDATE v = new.v"),
                {"k": key, "v": str(value)},
            )

    def delete(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM migration_state WHERE k = :k"), {"k": key})

    def age(self, key: str) -> float | None:
        """Seconds since `key` last changed, measured by the database clock (no app clock skew)."""
        with self.engine.connect() as conn:
            micros = conn.execute(
                text("SELECT TIMESTAMPDIFF(MICROSECOND, updated_at, CURRENT_TIMESTAMP(3)) FROM migration_state WHERE k = :k"),
                {"k": key},
            ).scalar()
        return None if micros is None else micros / 1_000_000

    def clear(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM migration_state"))

    def flags(self) -> Flags:
        return flags_from(self.all())

    def set_flag(self, name: str, on: bool) -> None:
        if name not in FLAG_NAMES:  # a typo check, not a check on the combination
            raise ValueError(f"unknown flag {name!r}; expected one of {', '.join(FLAG_NAMES)}")
        self.set(name, "on" if on else "off")

    def set_fault_rate(self, rate: float) -> None:
        """Demo scaffolding: see config.ENABLE_FAULT_INJECTION."""
        self.set("shadow_fault_rate", str(rate))


def flags_from(values: dict[str, str]) -> Flags:
    defaults = Flags()
    return replace(defaults, **{name: _parse(values.get(name), getattr(defaults, name)) for name in FLAG_NAMES})


class RuntimeSettings:
    """Cached view of the shared state, used by the app on every request."""

    def __init__(self, state: StateStore, ttl: float | None = None):
        self.state = state
        self.ttl = config.SETTINGS_TTL_SECONDS if ttl is None else ttl
        self._lock = threading.Lock()
        self._loaded_at = float("-inf")
        self._flags = Flags()
        self._fault_rate = 0.0

    def _refresh(self) -> None:
        if time.monotonic() - self._loaded_at < self.ttl:
            return
        with self._lock:
            if time.monotonic() - self._loaded_at < self.ttl:
                return
            values = self.state.all()
            self._flags = flags_from(values)
            self._fault_rate = float(values.get("shadow_fault_rate", "0") or 0)
            self._loaded_at = time.monotonic()

    def flags(self) -> Flags:
        self._refresh()
        return self._flags

    def fault_rate(self) -> float:
        self._refresh()
        return self._fault_rate


class FixedSettings:
    """In-memory settings for tests."""

    def __init__(self, flags: Flags | None = None, fault_rate: float = 0.0):
        self.current = flags or Flags()
        self.rate = fault_rate

    def flags(self) -> Flags:
        return self.current

    def fault_rate(self) -> float:
        return self.rate
