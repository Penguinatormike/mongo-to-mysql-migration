# AGENTS.md

Guidance for coding agents (and humans) working in this repo. The README explains *what* the migration does and why; this file covers how to change the code safely.

**This is a demo, not production code.** It's a small, runnable recreation of a real migration, meant to be read and run in minutes. When making changes:

- Keep it small and readable. Don't add production hardening (auth, retries, metrics backends, schema migration tooling, multi-collection support) unless asked; the README's "What's simplified" section lists those gaps on purpose. If you close one, update that list.
- Do keep it *correct*. The invariants below are the point of the demo, and the demo run must still end in `PASS`.

## Setup and commands

Tests are integration tests. They need real MongoDB and MySQL on localhost (defaults in `migrator/config.py`). There are no mocks for the databases.

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # once (Windows: .venv\Scripts\pip)
docker compose up -d mongo mysql                # just the databases, for local tests
python -m pytest                                # full suite (uses the `testdb` database in both)
python scripts/check_coverage.py                # tests + per-file coverage gate (what the hook runs)
docker compose up -d --build                    # databases + API on :8000
docker compose run --rm --build tools demo      # end-to-end migration under load (~1 min); --build because `up --build` skips the tools image
```

On Windows use `.venv\Scripts\python`. Tests never touch `appdb`, so running them doesn't disturb a demo.

## Coverage rule (enforced)

**Every core migration file must stay at or above 80% line coverage**, and new code in those files needs tests that keep it there.

- **Core** = everything under `migrator/` except the CLI (`__main__.py`) and the `demo/` package, as listed in `omit` in `.coveragerc`. A new module is core by default. Demo-only code (anything the demo run exercises but the app doesn't need) goes in `migrator/demo/`, which keeps it out of the gate.
- The threshold applies **per file**, not to the total, so a well-tested file can't hide an untested one.
- `.githooks/pre-commit` runs `scripts/check_coverage.py --min 80` whenever a commit touches `migrator/`, `tests/` or `scripts/` Python files, or `.coveragerc`. It blocks the commit if tests fail or any core file is below 80%. Enable it once per clone:
  ```sh
  git config core.hooksPath .githooks
  ```
- CI (`.github/workflows/ci.yml`) runs the same gate on every push and pull request, plus the full demo, which must end in `PASS`.
- Don't lower the threshold, add core files to `omit`, or use `# pragma: no cover` to get past the gate. Write the test.

## Invariants: don't break these

1. **Both stores have the same interface.** `MongoStore` (`stores/mongo.py`) and `MySQLStore` (`stores/mysql.py`) are interchangeable as source or target. If you add or change a method on one, do the same on the other, and add a `@pytest.mark.parametrize("which", ["mongo", "mysql"])` test in `tests/test_stores.py`.
2. **Copies are last-writer-wins, and the reconciler is the safety net.** Shadow writes, the backfill and repairs all use `upsert` / `upsert_many`, which insert or overwrite unconditionally. Deletes are hard deletes, mirrored with `delete`. There are no versions or tombstones, on purpose: the real migration didn't use them. A write that races the backfill or a repair can leave a stale or resurrected copy; that's expected, and a reconcile repairs it. That's why the runbook reconciles until clean before every flip that depends on the copy. Don't add conditional writes without being asked.
3. **Shadow writes never fail or stall a request** (`repository._shadow`). They log, increment a counter and move on; the reconciler repairs anything that was missed. A shadow that isn't serving reads is written through a `fast_shadows` client built with `timeout=config.SHADOW_TIMEOUT_SECONDS`; when MySQL is the shadow and `read_mysql` is on, it serves reads, so it keeps the normal client (`AccountRepository.shadow_store`).
4. **Each repository operation reads the flags once**, so a flag flip can't split a single request across stores.
5. **The backfill only runs while Mongo is primary and MySQL is written** (`write_mysql` on, `mysql_primary` off), and only after `write_mysql` has been on for 2× the settings TTL, so every app instance is already dual-writing (`wait_for_flag_rollout`). A finished run clears its checkpoint.
6. **A delete is always mirrored**, even when the primary had nothing to delete, because a stray copy in the shadow is drift. `delete()` returns whether the record existed.
7. **Fault injection is demo scaffolding, not part of the migration.** `repository` ignores `shadow_fault_rate` unless `ENABLE_FAULT_INJECTION=1` (set in `docker-compose.yml`). Tests opt in with `AccountRepository(..., fault_injection=True)`.
8. **The reconciler re-reads suspects before calling them drift**, and repairs re-read the source of truth before writing. Differences that disappear count as `transient`.
9. **Timestamps have millisecond precision** (`models.utcnow` / `truncate_ms`), matching Mongo and `DATETIME(3)`. Use `utcnow()`, not `datetime.now()`, or fingerprints won't match across stores.
10. **`Account.fingerprint()` is the cross-store equality check.** If you add a field to `Account`, add it to `to_doc`/`from_doc`, `params`/`from_row`, the MySQL `SCHEMA`, `_COLUMNS`/`_VALUES`, `_UPSERT`, the `UPDATE` in `MySQLStore.update`, `to_api` and `fingerprint`.
11. **Flags are independent and unvalidated, on purpose.** Like a real flag service, any combination can be set (`StateStore.set_flag` only rejects unknown names). Don't add combination checks; routing must do something well-defined for every combination (`Flags.primary` / `shadow` / `reads`), and the primary is always written.
12. **`app.py` knows nothing about the migration.** Flag routing belongs in `migration/repository.py`.

## Writing tests

- Tests are one file per module: `test_stores.py`, `test_flags.py`, `test_repository.py`, `test_backfill.py`, `test_reconcile.py`, `test_api.py`, `test_models.py`. Put a new test next to the module it exercises.
- Use the `stores` fixture (`tests/conftest.py`). It returns `(mongo, mysql, state)` and empties both databases and the migration state before each test.
- Use `make_account(**overrides)` for records, and `make_repo(stores, flags, fault_rate)` (or `FixedSettings`) to pin the repository to a flag combination. `conftest.py` also has the runbook's usual combinations (`BEFORE`, `DUAL_WRITE`, `READ_MYSQL`, `MYSQL_PRIMARY`, `CUT_OVER`) and `apply_flags(state, flags)` for tests that go through the shared state. Use `RuntimeSettings` only when you are testing the cache itself.
- Pass `settle=0` to `reconcile()` and `propagation=0` to `run_backfill()` with a `state` unless the test is about that wait.
- Use the `hung_server` fixture (a port that accepts connections and never answers) to test timeouts against a hung database.
- To simulate races (an in-flight write, a failing target), wrap a real store and override one method. `_LaggingTarget` in `tests/test_reconcile.py` and `_WriteAfterRead` in `tests/test_backfill.py` show the pattern. Prefer this to mocking the database client.
- Tests that sleep should use a short TTL or settle (≤0.25s). The whole suite should stay fast.

## Conventions

- Python 3.12, `from __future__ import annotations`, type hints on public functions.
- Raw SQL through SQLAlchemy `text()` (no ORM). Keyset pagination on `_id`/`id` for anything that walks a whole table.
- Keep the README's "Layout" section in sync when you add or rename modules.
- `coverage` is pinned in `requirements.txt` alongside `pytest`. Add new dependencies there with an exact version.
