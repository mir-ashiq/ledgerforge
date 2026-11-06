# LedgerForge migrations

Alembic revision history for the database schema. Policy and rationale:
**docs/adr/006-migration-baseline.md**. Short version:

- `0001_baseline` embeds `db/schema.sql` + `db/functions.sql` verbatim, so
  `alembic upgrade head` produces exactly the structure the SQL bootstrap
  produces. `tests/test_migrations.py` compares both structurally and fails
  CI on any drift.
- **Pre-1.0 (current)**: schema changes still land in `db/schema.sql` /
  `db/functions.sql` (kept idempotent), then the baseline is regenerated:

  ```
  make baseline-regen        # runs scripts/regen_baseline.py
  ```

- **Post-1.0**: `0001_baseline` is frozen. A schema change ships as a new
  revision *and* updates `db/schema.sql` in the same commit (the flattened
  current state), so fresh bootstraps and migrated databases stay identical:

  ```
  make migration-new rev=0002_add_periods m="add periods table"
  # edit db/migrations/versions/0002_add_periods.py (op.execute raw SQL)
  # update db/schema.sql + db/functions.sql to match
  make test                  # drift guard must stay green
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
