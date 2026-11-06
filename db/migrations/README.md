# LedgerForge migrations

Alembic revision history for the database schema. Policy and rationale:
**docs/adr/006-migration-baseline.md**. Short version:

- `0001_baseline` embeds `db/schema.sql` + `db/functions.sql` verbatim, so
  `alembic upgrade head` produces exactly the structure the SQL bootstrap
  produces. `tests/test_migrations.py` compares both structurally and fails
  CI on any drift.
- **The baseline is frozen** (ADR-006, amended 2026-11-06). The first
  forward revisions — `0002_multi_currency` (issue #4) and
  `0003_period_locking` (issue #5) — shipped against it, so it can never be
  regenerated. `db/migrations/baseline/` preserves the schema as it stood at
  the freeze; `make baseline-check` proves `0001` still embeds it exactly.

- A schema change ships as a new revision *and* updates `db/schema.sql` +
  `db/functions.sql` in the same commit (the flattened current state), so
  fresh bootstraps and migrated databases stay identical:

  ```
  make migration-new rev=0004_your_change m="describe it"
  # edit db/migrations/versions/0004_your_change.py (op.execute raw SQL)
  # update db/schema.sql + db/functions.sql to match
  # include the ledgerforge_app GRANTs for new tables and sequences
  make test                  # drift guard must stay green
  make baseline-check        # 0001 still matches the frozen snapshot
  ```

- `python -m ledgerforge.bootstrap` (the Docker/deploy path) stamps the
  `alembic_version` table to head after applying the SQL files, so operators
  can switch to `alembic upgrade head` at any time without re-running DDL:
  fresh installs converge on either path.

## Commands

```
alembic upgrade head                     # apply all revisions
alembic current                          # where this database stands
alembic downgrade 0001_baseline          # revert to baseline
python -m ledgerforge.migrations upgrade head   # same, from any install
```

Seed data (the TallyPrime chart of accounts) is deliberately **not** part of
any migration — it is data, not schema; `python -m ledgerforge.bootstrap`
applies it idempotently on top of either path.
