"""Reset both databases and fill MongoDB with the 'legacy' data set."""

from __future__ import annotations

import random
from datetime import timedelta

from bson import ObjectId

from ..migration.flags import StateStore
from ..models import PLANS, Account, truncate_ms, utcnow
from ..stores import MongoStore, MySQLStore


def reset(mongo: MongoStore, mysql: MySQLStore, state: StateStore) -> None:
    mongo.reset()
    mysql.create_schema()
    mysql.reset()
    state.clear()


def seed(mongo: MongoStore, count: int, batch_size: int = 1000, rng_seed: int = 42) -> int:
    rng = random.Random(rng_seed)
    now = utcnow()
    batch: list[dict] = []
    for i in range(count):
        created = truncate_ms(now - timedelta(seconds=rng.randint(86_400, 3 * 365 * 86_400)))
        updated = truncate_ms(created + timedelta(seconds=rng.randint(0, 30 * 86_400)))
        account = Account(
            id=str(ObjectId()),
            email=f"user{i}@example.com",
            display_name=f"User {i}",
            plan=rng.choices(PLANS, [70, 25, 5])[0],
            settings={
                "theme": rng.choice(["light", "dark", "system"]),
                "notifications": rng.random() < 0.6,
                "timezone": rng.choice(["America/Vancouver", "America/Toronto", "Europe/London", "Asia/Tokyo"]),
            },
            created_at=created,
            updated_at=updated,
        )
        batch.append(MongoStore.to_doc(account))
        if len(batch) >= batch_size:
            mongo.coll.insert_many(batch, ordered=False)
            batch = []
    if batch:
        mongo.coll.insert_many(batch, ordered=False)
    return count
