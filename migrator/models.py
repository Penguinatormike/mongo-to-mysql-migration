from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

PLANS = ("free", "pro", "enterprise")


def truncate_ms(dt: datetime) -> datetime:
    """MongoDB stores datetimes with millisecond precision; MySQL uses DATETIME(3) to match."""
    return dt.replace(microsecond=dt.microsecond - dt.microsecond % 1000)


def utcnow() -> datetime:
    return truncate_ms(datetime.now(timezone.utc).replace(tzinfo=None))


def _ts(dt: datetime) -> str:
    return truncate_ms(dt).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Account:
    """The record being migrated."""

    id: str
    email: str
    display_name: str
    plan: str
    settings: dict
    created_at: datetime
    updated_at: datetime

    def to_api(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "plan": self.plan,
            "settings": self.settings,
            "created_at": _ts(self.created_at) + "Z",
            "updated_at": _ts(self.updated_at) + "Z",
        }

    def fingerprint(self) -> str:
        """A store-independent hash, so the reconciler can compare a Mongo document with a MySQL row."""
        payload = {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "plan": self.plan,
            "settings": self.settings,
            "created_at": _ts(self.created_at),
            "updated_at": _ts(self.updated_at),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
