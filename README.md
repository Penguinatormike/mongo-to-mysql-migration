# Zero-downtime MongoDB → MySQL migration

[![CI](https://github.com/Penguinatormike/mongo-to-mysql-migration/actions/workflows/ci.yml/badge.svg)](https://github.com/Penguinatormike/mongo-to-mysql-migration/actions/workflows/ci.yml)

A runnable recreation of a production migration I led: moving a service's data out of MongoDB and into MySQL **while the service kept serving traffic**. That consolidated the service onto a single database, which saved roughly $60K a year in infrastructure.

> **This is a demo, not production code.** It rebuilds the approach at small scale so it can be run and read in a few minutes. It takes shortcuts a real deployment couldn't; they're listed under [What's simplified](#whats-simplified).

The production system can't be shared, so this repo rebuilds the same approach around a small accounts API:

- **Dual writes**, switched on and off with **feature flags**, one flag per step.
- A **resumable backfill** that's safe to run against live writes.
- A **reconciler** that finds drift and repairs it.
- A **load generator** that checks every response, so "zero downtime" is measured rather than claimed.

```
docker compose up -d --build
docker compose run --rm --build tools demo
```

## What the demo shows

The demo seeds 20,000 "legacy" accounts in MongoDB and starts 8 concurrent clients. It then flips the migration flags one at a time, with no pause in traffic.

During dual writes the demo makes 5% of the writes to MySQL fail on purpose. **This fault injection is demo scaffolding, not part of the migration**: locally nothing fails on its own, so without it the reconciler would have nothing to catch. In production the drift came from real failures (timeouts, deploys mid-request). Every bit of drift in the demo comes from those injected failures: run `demo --fault-rate 0` and the first reconcile finds none. Failed shadow deletes dominate (`extra`) because nothing heals them, while a failed create or update is usually overwritten by the record's next write.

```
== 2. Dual writes ========================================================
  flag write_mysql -> on: every write also goes to MySQL
  (demo-only fault injection: 5% of those MySQL writes are made to fail on purpose)

== 3. Backfill existing documents ========================================
  copied 20,599 documents in 42 batches (15.02s)

== 4. Reconcile (verify only) ============================================
  checked=20,677 missing=3 mismatched=6 extra=46 transient=6 repaired=0

== 5. Stop injecting faults, repair until clean ==========================
  pass 1: checked=20,740 missing=3 mismatched=8 extra=47 transient=3 repaired=58
  pass 2: checked=20,749 missing=0 mismatched=0 extra=0 transient=0 repaired=0

== Result ================================================================
  after                 requests   errors  inconsistent
  before                   2,301        0             0
  write_mysql on          12,448        0             0
  read_mysql on            1,639        0             0
  mysql_primary on         1,831        0             0
  write_mongo off          1,302        0             0
  total                   19,521        0             0
  latency p50=11.2ms p99=82.6ms
  shadow writes: 7,105 ok, 249 failed (demo fault injection)

  PASS: zero failed requests, zero inconsistent reads, no drift
```

## The migration, flag by flag

The migration is driven by four independent feature flags, the way it was in production. They live in a `migration_state` table standing in for a feature-flag service, and each app instance re-reads them every 0.5s, so every step is a flag flip, not a deploy.

| Flag | Default | When on |
|---|---|---|
| `write_mysql` | off | Every write also goes to MySQL |
| `read_mysql` | off | Reads are served from MySQL instead of MongoDB |
| `mysql_primary` | off | MySQL is the source of truth: a write must succeed there first, and MongoDB becomes the copy |
| `write_mongo` | on | Writes keep going to MongoDB. Turned off at the very end |

The usual order, one flip at a time:

```mermaid
flowchart LR
    A[before] -->|write_mysql on| B[dual writes]
    B -->|backfill + reconcile, then read_mysql on| C[reads from MySQL]
    C -->|mysql_primary on| D[MySQL is primary]
    D -->|reconcile, then write_mongo off| E[cut over]
```

| After flipping | Writes must succeed in | Also written to | Reads from | To roll back |
|---|---|---|---|---|
| (nothing yet) | MongoDB | — | MongoDB | — |
| `write_mysql` on | MongoDB | MySQL | MongoDB | Flip it off |
| `read_mysql` on | MongoDB | MySQL | **MySQL** | Flip it off; Mongo is still the source of truth |
| `mysql_primary` on | **MySQL** | MongoDB | MySQL | Reconcile MongoDB from MySQL, then flip it off |
| `write_mongo` off | MySQL | — | MySQL | Mongo can be decommissioned |

**Nothing checks that the combination makes sense**, as with a real flag service: you can turn on `read_mysql` before anything writes to MySQL, and reads will miss. Keeping the flags in a sane order is the operator's job, and every flip should follow a clean reconcile. The database named by `mysql_primary` is always written, whatever its own `write_*` flag says, so a write always lands somewhere.

## How it stays correct

**Every record carries a `version`.** Each write increments it. Writes to the secondary copy and the backfill both use a *version-guarded upsert*, which only replaces a row if the incoming version is newer ([`stores/`](migrator/stores/)). Without it, a backfill batch read just before a live update would overwrite that update with stale data.

**Deletes are versioned too.** A delete keeps the record as a *tombstone* with a bumped version, and the secondary gets the tombstone through the same guard. So a backfill batch read just before a delete, or an update's shadow write that arrives after the delete's, can't bring the record back: its version is lower than the tombstone's.

**Shadow writes never fail a request.** The primary write has already succeeded. If the shadow write fails, the app logs and counts it and moves on ([`migration/repository.py`](migrator/migration/repository.py)). Drift is the reconciler's job, not the user's problem. Shadow writes to a database that isn't serving reads go through clients with a short timeout (`SHADOW_TIMEOUT_SECONDS`, default 1s), so a hung secondary can't stall requests either.

**Dual writes start before the backfill.** That way, anything written after the backfill passes a record still reaches MySQL. The backfill refuses to run unless `write_mysql` is on and `mysql_primary` is off, and it waits until every app instance has picked up the `write_mysql` flip (instances cache flags for 0.5s). It walks MongoDB in `_id` order and checkpoints its progress, so a crash or pause resumes where it stopped. A finished run clears the checkpoint ([`migration/backfill.py`](migrator/migration/backfill.py)).

**The reconciler checks both directions** ([`migration/reconcile.py`](migrator/migration/reconcile.py)):

- *missing*: in the source but not the target (a failed shadow create).
- *mismatched*: content differs, found by comparing a store-independent SHA-256 fingerprint of each record (a failed shadow update).
- *extra*: in the target but not the source (a failed shadow delete).

With live traffic, a write can land between reading the two stores, so every suspect is re-read after a short pause. Only differences that persist count as drift (shown as `transient` otherwise). Repairs re-read the source of truth before writing, and passes repeat until one comes back clean.

**The load generator checks that every response is correct, not just that it came back** ([`demo/loadgen.py`](migrator/demo/loadgen.py)). Each client owns the accounts it creates, so it knows the right answer. It flags:

- any non-2xx response or connection error;
- a read that returns an older version than the client last wrote;
- an update that doesn't bump the version by exactly one;
- an account that's still readable after its delete.

## Running it yourself

You only need Docker. Everything else runs in containers.

```bash
docker compose up -d --build                                 # MongoDB, MySQL and the API on :8000
docker compose run --rm --build tools demo                   # the whole migration under load (~1 min)
docker compose run --rm --build --entrypoint pytest tools    # the integration tests
```

To drive the steps by hand:

```bash
docker compose run --rm tools seed --count 50000
docker compose run --rm tools flag write_mysql on
docker compose run --rm tools backfill --pause 0.05
docker compose run --rm tools reconcile                # verify only
docker compose run --rm tools reconcile --repair
docker compose run --rm tools flag read_mysql on
docker compose run --rm tools flag                    # show every flag
docker compose run --rm tools status
```

Running `loadgen --duration 60` in another terminal sends checked traffic while you do it.

`--build` matters on `run`: the `tools` image sits behind a compose profile, so `up --build` doesn't rebuild it, and without the flag it can run stale code after a change. `seed` and `demo` wipe both databases they're pointed at.

## Layout

```
migrator/
  app.py           FastAPI accounts service (knows nothing about the migration)
  models.py        the Account record and its cross-store fingerprint
  config.py        connection settings, timeouts, demo switches (env vars)
  __main__.py      the CLI (python -m migrator ...)
  stores/
    mongo.py       MongoStore  } same interface, so either can be
    mysql.py       MySQLStore  } the source or the target
  migration/
    flags.py       the feature flags and the cached view the app reads
    repository.py  flag-driven routing + shadow writes
    backfill.py    resumable, version-guarded bulk copy
    reconcile.py   two-way diff + repair
  demo/            demo-only code, outside the coverage gate
    run.py         the end-to-end run
    loadgen.py     self-checking traffic
    seed.py        the "legacy" data set
tests/             integration tests against real MongoDB and MySQL, one file per module
scripts/check_coverage.py   per-file coverage gate (80% on core files), run by .githooks/pre-commit
AGENTS.md          rules for changing the code: invariants, tests, coverage
```

## What's simplified

This is a demo. These are the known gaps between it and something you'd run in production, most of them deliberate to keep it small:

- **The feature flags live in MySQL**, standing in for a feature-flag service. If MySQL is down the app can't read its flags and requests fail, even before the migration starts. A real flag client would keep serving the last known value.
- **Changing which database is primary isn't fenced.** During the 0.5s it takes every instance to pick up a `mysql_primary` flip, an in-flight request can still write to the old primary. The demo relies on watching the logs and metrics and flipping only after a clean reconcile; a real system would add a brief write freeze or reject writes made under stale flags.
- **Failed shadow writes are only counted.** Finding them again means a full reconcile of both stores. At scale you'd record the failed IDs (an outbox) or replace dual writes with change data capture (MongoDB change streams).
- **A reconciler repair can briefly overwrite a newer copy with an older one** if a live write lands mid-repair. The next pass fixes it, which is why the demo repairs until a pass comes back clean.
- **Tombstones are never purged.** A real cutover would purge them once `write_mongo` is off for good.
- **No auth, hard-coded credentials, schema created on startup** instead of with a migration tool, and the demo's fault injection (scaffolding, not part of the migration) sits in the request path, switched off unless `ENABLE_FAULT_INJECTION=1`.
- **One collection and one app instance.** The real migration moved about ten collections across many instances, with:
  - per-collection flags, each collection going through these steps on its own;
  - metrics and alerts on the shadow-write failure rate and the drift count, instead of printed counters;
  - the reconciler running on a schedule during dual writes, not only before each flag flip;
  - read comparison while `write_mysql` is on (read both stores, serve Mongo, log any differences) to build confidence before moving reads.

## License

[MIT](LICENSE)
