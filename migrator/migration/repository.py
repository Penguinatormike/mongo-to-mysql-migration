"""Flag-driven data access: the only place the application decides which database to talk to."""

from __future__ import annotations

import logging
import random
import threading
from collections import Counter

from bson import ObjectId

from .. import config
from ..models import Account, utcnow

log = logging.getLogger(__name__)


class InjectedFault(RuntimeError):
    """Demo scaffolding, not part of the migration: a simulated shadow-write failure, so the demo has real
    drift for the reconciler to catch. Only raised when ENABLE_FAULT_INJECTION is on (see config.py)."""


class AccountRepository:
    def __init__(self, mongo, mysql, settings, fast_shadows: dict | None = None, fault_injection: bool | None = None):
        """`fast_shadows` maps a store name to a client with short timeouts (config.SHADOW_TIMEOUT_SECONDS),
        used for shadow writes to a database that isn't serving reads, so a hung secondary costs a request
        at most that timeout. `fault_injection` (demo scaffolding) defaults to config.ENABLE_FAULT_INJECTION."""
        self.stores = {"mongo": mongo, "mysql": mysql}
        self.fast_shadows = fast_shadows or {}
        self.settings = settings
        self.fault_injection = config.ENABLE_FAULT_INJECTION if fault_injection is None else fault_injection
        self._counters: Counter = Counter()
        self._lock = threading.Lock()

    def _incr(self, key: str) -> None:
        with self._lock:
            self._counters[key] += 1

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    # Each operation reads the flags once, so a flag flip never splits a single request.

    def get(self, id: str) -> Account | None:
        flags = self.settings.flags()
        return self.stores[flags.reads].get(id)

    def create(self, fields: dict) -> Account:
        flags = self.settings.flags()
        ts = utcnow()
        account = Account(id=str(ObjectId()), created_at=ts, updated_at=ts, **fields)
        self.stores[flags.primary].create(account)
        self._shadow(flags, "upsert", account)
        return account

    def update(self, id: str, changes: dict) -> Account | None:
        flags = self.settings.flags()
        account = self.stores[flags.primary].update(id, changes)
        if account is not None:
            self._shadow(flags, "upsert", account)
        return account

    def delete(self, id: str) -> bool:
        flags = self.settings.flags()
        deleted = self.stores[flags.primary].delete(id)
        self._shadow(flags, "delete", id)  # even if the primary had nothing: a stray shadow copy is drift
        return deleted

    def shadow_store(self, flags):
        """The client for shadow writes under these flags. A shadow that also serves reads (read_mysql on,
        MySQL not yet primary) keeps the normal client, because dropping its writes would show users stale data."""
        if flags.shadow == flags.reads:
            return self.stores[flags.shadow]
        return self.fast_shadows.get(flags.shadow, self.stores[flags.shadow])

    def _shadow(self, flags, method: str, *args) -> None:
        """Mirror a write to the secondary store. Failures are logged and counted, never raised:
        the primary write already succeeded, and the reconciler repairs whatever is missed."""
        if flags.shadow is None:
            return
        try:
            if self.fault_injection:  # demo scaffolding only; never on outside the demo
                rate = self.settings.fault_rate()
                if rate and random.random() < rate:
                    raise InjectedFault("injected shadow write failure")
            getattr(self.shadow_store(flags), method)(*args)
            self._incr("shadow_writes_ok")
        except Exception as exc:  # noqa: BLE001 - shadow writes must never fail the request
            self._incr("shadow_writes_failed")
            log.warning("shadow %s to %s failed: %s", method, flags.shadow, exc)
