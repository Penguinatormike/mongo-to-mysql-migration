"""MongoDB store. Shares its interface with MySQLStore, so the migration code can treat either as source or target."""

from __future__ import annotations

from typing import Iterable, Iterator

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument

from .. import config
from ..models import Account, utcnow


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
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        }

    # --- application operations -------------------------------------------------

    def get(self, id: str) -> Account | None:
        oid = self._oid(id)
        doc = self.coll.find_one({"_id": oid}) if oid else None
        return self.from_doc(doc) if doc else None

    def create(self, a: Account) -> None:
        self.coll.insert_one(self.to_doc(a))

    def update(self, id: str, changes: dict) -> Account | None:
        oid = self._oid(id)
        if oid is None:
            return None
        doc = self.coll.find_one_and_update(
            {"_id": oid},
            {"$set": {**changes, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return self.from_doc(doc) if doc else None

    def delete(self, id: str) -> bool:
        """Returns True if there was a record to delete."""
        oid = self._oid(id)
        return oid is not None and self.coll.delete_one({"_id": oid}).deleted_count == 1

    # --- migration operations ---------------------------------------------------

    def upsert(self, a: Account) -> None:
        """Write this exact copy, inserting or overwriting. Last writer wins."""
        self.coll.replace_one({"_id": ObjectId(a.id)}, self.to_doc(a), upsert=True)

    def upsert_many(self, accounts: Iterable[Account]) -> None:
        for a in accounts:
            self.upsert(a)

    def get_many(self, ids: list[str]) -> dict[str, Account]:
        oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
        if not oids:
            return {}
        return {str(d["_id"]): self.from_doc(d) for d in self.coll.find({"_id": {"$in": oids}})}

    def iter_batches(self, batch_size: int = 1000, after_id: str | None = None) -> Iterator[list[Account]]:
        """Walk every record in _id order. Keyset pagination keeps every batch cheap and makes the walk resumable."""
        while True:
            query = {"_id": {"$gt": ObjectId(after_id)}} if after_id else {}
            docs = list(self.coll.find(query).sort("_id", ASCENDING).limit(batch_size))
            if not docs:
                return
            batch = [self.from_doc(d) for d in docs]
            yield batch
            after_id = batch[-1].id
            if len(docs) < batch_size:
                return

    def count(self) -> int:
        return self.coll.count_documents({})

    def reset(self) -> None:
        self.coll.drop()
