"""Synthetic production traffic. Every request is checked, so "zero downtime" is measured, not claimed.

Each worker owns the accounts it creates, so it knows exactly what a correct response looks like:
  * any non-2xx response or connection error counts as an error;
  * reading back an older version than the worker last wrote counts as a consistency error;
  * an update that doesn't bump the version by exactly one counts as a consistency error;
  * an account that's still readable after its delete counts as a consistency error.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx


@dataclass
class LoadStats:
    label: str = "-"
    requests: int = 0
    errors: int = 0
    consistency_errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    by_step: dict[str, Counter] = field(default_factory=dict)  # keyed by `label`
    samples: list[str] = field(default_factory=list)

    def _step(self) -> Counter:
        return self.by_step.setdefault(self.label, Counter())

    def record(self, ms: float, ok: bool = True, note: str | None = None) -> None:
        self.requests += 1
        self.latencies_ms.append(ms)
        c = self._step()
        c["requests"] += 1
        if not ok:
            self.errors += 1
            c["errors"] += 1
            self._sample(note)

    def inconsistent(self, note: str) -> None:
        self.consistency_errors += 1
        self._step()["consistency_errors"] += 1
        self._sample(note)

    def _sample(self, note: str | None) -> None:
        if note and len(self.samples) < 20:
            self.samples.append(note)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        data = sorted(self.latencies_ms)
        return data[min(len(data) - 1, int(p / 100 * len(data)))]


async def _call(client: httpx.AsyncClient, stats: LoadStats, method: str, url: str, expect: int, **kw):
    started = time.perf_counter()
    try:
        resp = await client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        stats.record((time.perf_counter() - started) * 1000, ok=False, note=f"{method} {url}: {exc!r}")
        return None
    ms = (time.perf_counter() - started) * 1000
    if resp.status_code != expect:
        stats.record(ms, ok=False, note=f"{method} {url} -> {resp.status_code} {resp.text[:120]}")
        return None
    stats.record(ms)
    return resp


OPS = ["create", "read_own", "read_seed", "update", "delete"]
WEIGHTS = [15, 30, 20, 25, 10]


async def _worker(client, stats, stop, rng: random.Random, seed_ids: list[str], think: float) -> None:
    owned: dict[str, int] = {}  # account id -> last version this worker wrote
    while not stop.is_set():
        op = rng.choices(OPS, WEIGHTS)[0]
        if not owned:
            op = "create"
        if op == "read_seed" and not seed_ids:
            op = "read_own"
        if op == "delete" and len(owned) < 10:
            op = "update"

        if op == "create":
            body = {
                "email": f"load{rng.randrange(10**9)}@example.com",
                "display_name": f"Load User {rng.randrange(10**6)}",
                "plan": rng.choice(["free", "pro", "enterprise"]),
                "settings": {"theme": rng.choice(["light", "dark"]), "beta": rng.random() < 0.3},
            }
            r = await _call(client, stats, "POST", "/accounts", 201, json=body)
            if r is not None:
                owned[r.json()["id"]] = r.json()["version"]

        elif op == "read_own":
            id = rng.choice(list(owned))
            r = await _call(client, stats, "GET", f"/accounts/{id}", 200)
            if r is not None and r.json()["version"] != owned[id]:
                stats.inconsistent(f"stale read {id}: got v{r.json()['version']}, wrote v{owned[id]}")

        elif op == "read_seed":
            await _call(client, stats, "GET", f"/accounts/{rng.choice(seed_ids)}", 200)

        elif op == "update":
            id = rng.choice(list(owned))
            body = {"display_name": f"Load User {rng.randrange(10**6)}", "settings": {"theme": rng.choice(["light", "dark"])}}
            r = await _call(client, stats, "PATCH", f"/accounts/{id}", 200, json=body)
            if r is not None:
                version = r.json()["version"]
                if version != owned[id] + 1:
                    stats.inconsistent(f"version skip {id}: v{owned[id]} -> v{version}")
                owned[id] = version

        elif op == "delete":
            id = rng.choice(list(owned))
            r = await _call(client, stats, "DELETE", f"/accounts/{id}", 204)
            if r is not None:
                del owned[id]
                await _call(client, stats, "GET", f"/accounts/{id}", 404)

        if think:
            await asyncio.sleep(think)


async def run_load(
    app_url: str,
    stop: asyncio.Event,
    workers: int = 8,
    seed_ids: list[str] | None = None,
    think_ms: float = 5,
    stats: LoadStats | None = None,
    rng_seed: int | None = None,
) -> LoadStats:
    stats = stats or LoadStats()
    base = random.Random(rng_seed)
    limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers * 2)
    async with httpx.AsyncClient(base_url=app_url, timeout=10, limits=limits) as client:
        await asyncio.gather(
            *(
                _worker(client, stats, stop, random.Random(base.random()), list(seed_ids or []), think_ms / 1000)
                for _ in range(workers)
            )
        )
    return stats
