"""MongoDB store. Shares its interface with MySQLStore, so the migration code can treat either as source or target.

Deletes are tombstones: the document is kept with `deleted: true` and a bumped version, so the version
guard also stops a stale copy (a backfill batch, a late shadow update) from bringing it back.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .. import config
from ..models import Account, utcnow

LIVE = {"deleted": {"$ne": True}}  # seeded documents have no `deleted` field at all


class MongoStore:
    name = "mongo"

    def __init__(self, url: str | None = None, db: str | None = None, timeout: float | None = None):
        """`timeout` (seconds) bounds every operation, including finding a server. Use it for shadow
        writes, so a hung secondary fails fast instead of stalling the request."""
        opts = {}
        if timeout is not None:
            ms = int(timeout * 1000)
            opts = {"timeoutMS": ms, "connectTimeoutMS": ms, "serverSelectionTimeoutMS": ms}
        self.client = MongoClient(url or config.MONGO_URL, tz_aware=False, maxPoolSize=50, **opts)
        self.db = self.client[db or config.MONGO_DB]
        self.coll = self.db["accounts"]

    @staticmethod
    def _oid(id: str) -> ObjectId | None:
        return ObjectId(id) if ObjectId.is_valid(id) else None

    @staticmethod
    def from_doc(doc: dict) -> Account:
        return Account(
            id=str(doc["_id"]),
            email=doc["email"],
            display_name=doc["display_name"],
            plan=doc["plan"],
            settings=doc.get("settings") or {},
            version=int(doc["version"]),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
        )

    @staticmethod
    def to_doc(a: Account) -> dict:
        return {
            "_id": ObjectId(a.id),
            "email": a.email,
            "display_name": a.display_name,
            "plan": a.plan,
            "settings": a.settings,
            "version": a.version,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # --- application operations -------------------------------------------------

    def get(self, id: str) -> Account | None:
        oid = self._oid(id)
        doc = self.coll.find_one({"_id": oid, **LIVE}) if oid else None
        return self.from_doc(doc) if doc else None

    def create(self, a: Account) -> None:
        self.coll.insert_one(self.to_doc(a))

    def update(self, id: str, changes: dict) -> Account | None:
        oid = self._oid(id)
        if oid is None:
            return None
        doc = self.coll.find_one_and_update(
            {"_id": oid, **LIVE},
            {"$set": {**changes, "updated_at": utcnow()}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        return self.from_doc(doc) if doc else None

    def delete(self, id: str) -> int | None:
        """Tombstone a live record. Returns the tombstone's version, or None if there was nothing to delete."""
        oid = self._oid(id)
        if oid is None:
            return None
        doc = self.coll.find_one_and_update(
            {"_id": oid, **LIVE},
            {"$set": {"deleted": True, "updated_at": utcnow()}, "$inc": {"version": 1}},
            projection={"version": 1},
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["version"]) if doc else None

    # --- migration operations ---------------------------------------------------

    def put_if_newer(self, a: Account) -> None:
        """Upsert only if the stored copy (live or tombstone) is older. A newer or equal copy makes the
        upsert's insert collide on _id, which means there is nothing to do."""
        try:
            self.coll.replace_one({"_id": ObjectId(a.id), "version": {"$lt": a.version}}, self.to_doc(a), upsert=True)
        except DuplicateKeyError:
            pass

    def put_if_newer_many(self, accounts: Iterable[Account]) -> None:
        for a in accounts:
            self.put_if_newer(a)

    def put_tombstone(self, id: str, version: int) -> None:
        """Mirror a delete. Version-guarded like put_if_newer, and creates the tombstone if the record
        isn't here yet, so a shadow write that arrives later can't create it either."""
        try:
            self.coll.update_one(
                {"_id": ObjectId(id), "version": {"$lt": version}},
                {"$set": {"deleted": True, "version": version, "updated_at": utcnow()}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass

    def force_put(self, a: Account) -> None:
        self.coll.replace_one({"_id": ObjectId(a.id)}, self.to_doc(a), upsert=True)

    def get_many(self, ids: list[str]) -> dict[str, Account]:
        oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
        if not oids:
            return {}
        return {str(d["_id"]): self.from_doc(d) for d in self.coll.find({"_id": {"$in": oids}, **LIVE})}

    def iter_batches(self, batch_size: int = 1000, after_id: str | None = None) -> Iterator[list[Account]]:
        """Walk live records in _id order. Keyset pagination keeps every batch cheap and makes the walk resumable."""
        while True:
            query = {"_id": {"$gt": ObjectId(after_id)}, **LIVE} if after_id else dict(LIVE)
            docs = list(self.coll.find(query).sort("_id", ASCENDING).limit(batch_size))
            if not docs:
                return
            batch = [self.from_doc(d) for d in docs]
            yield batch
            after_id = batch[-1].id
            if len(docs) < batch_size:
                return

    def count(self) -> int:
        return self.coll.count_documents(LIVE)

    def reset(self) -> None:
        self.coll.drop()
