"""
Versioned DDL migrations for the RiskBalancer database.

Each migration is a callable that takes a `sqlite3.Connection` and applies
schema changes. Migrations run in order and are tracked by SQLite's
`PRAGMA user_version`. The `schema_version` table records each application
with a timestamp for human inspection — it is created by migration 1.

Migrations are append-only: once a migration has been released and applied
on any developer's machine, it must never be edited. New changes are new
migrations.

Author: Emre Tezel
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, List

# Each migration applies one numbered change. The list order is the schema
# version order: index `i` is version `i + 1`. The runner reads
# `PRAGMA user_version` to find where to resume and refuses to start if the
# DB is newer than this binary knows about (downgrade protection).
Migration = Callable[[sqlite3.Connection], None]


# ---------------------------------------------------------------------------
# Migration 1: initial schema (Option B of the persistence redesign).
#
# Notes on the choices encoded here:
#
# - Every table is declared `STRICT` so SQLite actually enforces the column
#   type instead of accepting anything via type affinity. STRICT requires
#   SQLite >= 3.37; `db._require_modern_sqlite` rejects older versions before
#   we get here.
#
# - Money is stored as `INTEGER` ten-thousandths of a unit currency
#   (suffix `_decithou`) so we can keep four decimal places without floating
#   point error. £1.2345 is stored as 12345.
#
# - Fractions (weights, volatility, adjustments, FX rates) are stored as
#   `INTEGER` parts-per-million (suffix `_micros`). 0.55 is 550000; 1.0 is
#   1_000_000. We never store fractions as REAL.
#
# - Quantities (number of units of a holding) are stored as `INTEGER` micro-
#   units, allowing fractional shares to six decimal places.
#
# - Dates use ISO-8601 `TEXT` (`YYYY-MM-DD`) with a GLOB CHECK so the
#   database rejects malformed inputs at write time.
#
# - Timestamps use ISO-8601 UTC `TEXT` with the trailing `Z` suffix
#   (e.g. `2026-05-17T12:34:56Z`). The CHECK is loose on the time part
#   so fractional seconds and short forms both pass.
# ---------------------------------------------------------------------------

# Allowed adapters, in one place. The tuple is the authoritative form;
# `_ADAPTERS_LIST` is the SQL `IN (...)` literal derived from it for the
# `source.adapter` CHECK clause. `manual` covers user-entered holdings
# that did not originate from a broker statement.
KNOWN_ADAPTERS: tuple[str, ...] = (
    "ibkr",
    "ajbell",
    "citi",
    "ms401k",
    "schwab",
    "aegon",
    "manual",
)
_ADAPTERS_LIST = "(" + ",".join(f"'{a}'" for a in KNOWN_ADAPTERS) + ")"

# SQLite `GLOB` uses shell-style wildcards: `?` matches a single character
# and `*` matches zero or more. (`_` is literal in GLOB — it is the
# single-character wildcard in `LIKE`, not here.) We use GLOB rather than
# LIKE so the patterns are case-sensitive without depending on PRAGMA
# case_sensitive_like.
_TIMESTAMP_GLOB = "????-??-??T*Z"
_DATE_GLOB = "????-??-??"

_MIGRATION_1_TABLES: tuple[str, ...] = (
    # Records every applied migration with a timestamp. Used for human
    # inspection; the runner relies on `PRAGMA user_version` for sequencing.
    f"""
    CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL CHECK (applied_at GLOB '{_TIMESTAMP_GLOB}')
    ) STRICT
    """,
    # Users are the top-level namespace. `name` is the on-disk identifier
    # the CLI accepts in `--user`.
    f"""
    CREATE TABLE user (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE CHECK (length(name) > 0),
        created_at TEXT NOT NULL CHECK (created_at GLOB '{_TIMESTAMP_GLOB}')
    ) STRICT
    """,
    # Single hierarchical registry of categories. Pure structure — no
    # volatility, no adjustment, no weight. Leaf vs branch is a per-plan
    # concept (see `plan_node`).
    """
    CREATE TABLE category (
        id INTEGER PRIMARY KEY,
        parent_id INTEGER REFERENCES category(id) ON DELETE RESTRICT,
        name TEXT NOT NULL CHECK (length(name) > 0),
        UNIQUE (parent_id, name)
    ) STRICT
    """,
    # Authoritative per-category attributes drawn from the seed plan:
    #   * `weight_micros`   — parent-relative weight (siblings sum to 1.0
    #     within their parent in the seed model).
    #   * `volatility_micros`, `adjustment_micros` — populated on seed
    #     leaves only; NULL on seed branches. Branch-level vol/adj is
    #     computed on the fly as the weight-weighted average of children
    #     when a user's plan treats the branch as a leaf, so storing it
    #     here would duplicate a derived fact.
    # The two leaf-only columns are tied: either both are NULL (branch)
    # or both are non-NULL (leaf). One row per seed-plan category.
    """
    CREATE TABLE category_attribute (
        category_id INTEGER PRIMARY KEY REFERENCES category(id) ON DELETE CASCADE,
        weight_micros INTEGER NOT NULL
            CHECK (weight_micros >= 0 AND weight_micros <= 1000000),
        volatility_micros INTEGER
            CHECK (volatility_micros IS NULL OR volatility_micros >= 0),
        adjustment_micros INTEGER
            CHECK (adjustment_micros IS NULL OR adjustment_micros >= 0),
        CHECK ((volatility_micros IS NULL) = (adjustment_micros IS NULL))
    ) STRICT
    """,
    # A broker. One row per adapter globally — IBKR is IBKR regardless of
    # which user holds an account there. The adapter alone determines how
    # statements are parsed, so it has no business being per-user.
    f"""
    CREATE TABLE source (
        id INTEGER PRIMARY KEY,
        adapter TEXT NOT NULL UNIQUE CHECK (adapter IN {_ADAPTERS_LIST})
    ) STRICT
    """,
    # A named account at a broker, owned by a user (e.g. Emre's AJ Bell
    # ISA, Tani's AJ Bell SIPP). Two users with accounts at the same broker
    # share the same `source` row; ownership lives here. `source` is
    # `RESTRICT` because removing a broker while accounts still reference
    # it would silently strand history; `user` cascades so deleting a user
    # tears down their accounts (and, transitively, imports and positions).
    """
    CREATE TABLE account (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
        name TEXT NOT NULL CHECK (length(name) > 0),
        UNIQUE (user_id, source_id, name)
    ) STRICT
    """,
    # Global registry of broker instruments. The same ticker at different
    # brokers is two distinct rows — the natural key is
    # `(source_id, instrument_id_text)`. The adapter string lives on
    # `source.adapter` (single source of truth); we reach it from here via
    # the FK rather than duplicating the column.
    """
    CREATE TABLE instrument (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
        instrument_id_text TEXT NOT NULL CHECK (length(instrument_id_text) > 0),
        description TEXT,
        UNIQUE (source_id, instrument_id_text)
    ) STRICT
    """,
    # Instrument-to-category mappings, split-aware (multiple rows per
    # instrument when the holding maps across several categories). One row
    # per (instrument, category) pair. Mappings are global — the same
    # mapping applies to every user. The leaf-only invariant (a mapping
    # must target a category with no children) is enforced by triggers,
    # not by a CHECK, because CHECK cannot reference other rows.
    """
    CREATE TABLE mapping (
        id INTEGER PRIMARY KEY,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT,
        category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE RESTRICT,
        weight_micros INTEGER NOT NULL
            CHECK (weight_micros > 0 AND weight_micros <= 1000000),
        UNIQUE (instrument_id, category_id)
    ) STRICT
    """,
    # Historical FX rates, one row per (date, currency). The stored rate
    # is GBP per native unit (e.g. 0.76 for USD/GBP on a given day).
    f"""
    CREATE TABLE fx_rate (
        id INTEGER PRIMARY KEY,
        rate_date TEXT NOT NULL CHECK (rate_date GLOB '{_DATE_GLOB}'),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        gbp_rate_micros INTEGER NOT NULL CHECK (gbp_rate_micros > 0),
        UNIQUE (rate_date, currency)
    ) STRICT
    """,
    # A user's plan: a tree of category targets. The same category can be
    # a leaf for one user and a branch for another — leaf/branch status is
    # purely structural (no other `plan_node` row references this one as
    # parent). The application enforces that sibling weights at every
    # level sum to 1.0. Volatility and adjustment are NOT stored here —
    # they live on `category_attribute` and are looked up (or computed as
    # a weighted average of children when the plan terminates above the
    # seed's leaves) at report time.
    """
    CREATE TABLE plan_node (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES plan_node(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE RESTRICT,
        weight_micros INTEGER NOT NULL
            CHECK (weight_micros >= 0 AND weight_micros <= 1000000),
        UNIQUE (user_id, parent_id, category_id)
    ) STRICT
    """,
    # An import event: one row per (account, as_of). The owning user is
    # derivable via `account.user_id`, so it is not denormalised here.
    # Re-importing the same (account, as_of) is implemented as a
    # transactional DELETE then INSERT in the import command, cascading
    # through positions via ON DELETE CASCADE.
    f"""
    CREATE TABLE statement_import (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        as_of TEXT NOT NULL CHECK (as_of GLOB '{_DATE_GLOB}'),
        statement_path TEXT,
        imported_at TEXT NOT NULL CHECK (imported_at GLOB '{_TIMESTAMP_GLOB}'),
        UNIQUE (account_id, as_of)
    ) STRICT
    """,
    # A holding inside an import. Native amounts only; GBP is computed
    # at query time by joining `fx_rate` on currency and picking the
    # latest `rate_date <= statement_import.as_of`. `fx_rate` is treated
    # as append-only authoritative history, so no per-import snapshot
    # table is needed — that would just duplicate a fact already in
    # `fx_rate`.
    """
    CREATE TABLE position (
        id INTEGER PRIMARY KEY,
        statement_import_id INTEGER NOT NULL
            REFERENCES statement_import(id) ON DELETE CASCADE,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT,
        description TEXT,
        quantity_micro_units INTEGER,
        market_value_native_decithou INTEGER NOT NULL
            CHECK (market_value_native_decithou >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        UNIQUE (statement_import_id, instrument_id)
    ) STRICT
    """,
)

_MIGRATION_1_INDEXES: tuple[str, ...] = (
    # Drives the per-import mapping lookup: given an instrument parsed
    # from a statement, fetch all (category, weight) rows for it. One
    # key, no scope discrimination — mappings are global.
    "CREATE INDEX idx_mapping_instrument ON mapping(instrument_id)",
    # "Portfolio as of date X" queries scan `statement_import` by account.
    # The `UNIQUE (account_id, as_of)` constraint already produces a usable
    # index for `(account_id, as_of DESC)` lookups, so no additional index
    # is declared here. Per-user queries reach `statement_import` via
    # `account.user_id`, which is covered by the leading column of
    # `account.UNIQUE (user_id, source_id, name)`.
    # Supports cross-import "every holding of EMIM ever" queries used by
    # the interactive walker when suggesting categories.
    "CREATE INDEX idx_position_instrument ON position(instrument_id)",
    # SQLite treats NULL as distinct in composite UNIQUE constraints, so
    # `UNIQUE (parent_id, name)` does NOT prevent two top-level categories
    # from sharing a name (both have `parent_id IS NULL`). This partial
    # unique index closes that gap for the root-level case.
    "CREATE UNIQUE INDEX idx_category_top_level_name ON category(name) WHERE parent_id IS NULL",
)


_MIGRATION_1_TRIGGERS: tuple[str, ...] = (
    # Enforce the leaf-only invariant for mapping targets: a mapping must
    # not point at a category that currently has children. Triggers fire
    # on the data being written; a category that later gains children
    # does NOT retroactively invalidate existing mappings — the resolver
    # tolerates such "stale" rows by rolling up to the user's plan leaf
    # at report time. Strict enforcement happens at the time someone
    # introduces the inconsistency (insert / update of category_id),
    # which is where the user is in a position to fix it.
    """
    CREATE TRIGGER mapping_target_must_be_leaf_insert
    BEFORE INSERT ON mapping
    FOR EACH ROW
    WHEN EXISTS (SELECT 1 FROM category WHERE parent_id = NEW.category_id)
    BEGIN
        SELECT RAISE(ABORT, 'mapping target must be a leaf category');
    END
    """,
    """
    CREATE TRIGGER mapping_target_must_be_leaf_update
    BEFORE UPDATE OF category_id ON mapping
    FOR EACH ROW
    WHEN EXISTS (SELECT 1 FROM category WHERE parent_id = NEW.category_id)
    BEGIN
        SELECT RAISE(ABORT, 'mapping target must be a leaf category');
    END
    """,
)

_MIGRATION_1_VIEWS: tuple[str, ...] = (
    # The most-recent import per account. Used by `current_position` and
    # by the report writer.
    """
    CREATE VIEW current_import AS
    SELECT si.*
    FROM statement_import si
    WHERE si.as_of = (
        SELECT MAX(si2.as_of)
        FROM statement_import si2
        WHERE si2.account_id = si.account_id
    )
    """,
    # Positions tied to the current import per account. Reports join this
    # against `fx_rate` (filtered to the latest `rate_date <= as_of` for
    # each currency) to compute the GBP value. `user_id` is reached
    # through `account` — the import itself does not store it.
    """
    CREATE VIEW current_position AS
    SELECT
        p.id AS id,
        p.statement_import_id AS statement_import_id,
        p.instrument_id AS instrument_id,
        p.description AS description,
        p.quantity_micro_units AS quantity_micro_units,
        p.market_value_native_decithou AS market_value_native_decithou,
        p.currency AS currency,
        a.user_id AS user_id,
        ci.account_id AS account_id,
        ci.as_of AS as_of
    FROM position p
    JOIN current_import ci ON ci.id = p.statement_import_id
    JOIN account a ON a.id = ci.account_id
    """,
    # Recursive view materialising the full ' / '-joined path for every
    # category. Used wherever we have to render or match a path string.
    """
    CREATE VIEW category_path AS
    WITH RECURSIVE walk(id, parent_id, path) AS (
        SELECT id, parent_id, name
        FROM category
        WHERE parent_id IS NULL
        UNION ALL
        SELECT c.id, c.parent_id, walk.path || ' / ' || c.name
        FROM category c
        JOIN walk ON c.parent_id = walk.id
    )
    SELECT id, path FROM walk
    """,
)


def _migration_1(connection: sqlite3.Connection) -> None:
    """Create the initial schema: tables, reference data, indexes, views, triggers."""
    for statement in _MIGRATION_1_TABLES:
        connection.execute(statement)
    # Reference data: one row per known adapter. `source` is fixed
    # configuration — every supported broker has a row. `instrument.source_id`,
    # `account.source_id`, and any future per-broker FK rely on these
    # being present from the moment the schema exists. We sort the list so
    # the assigned surrogate ids are stable across machines.
    for adapter_name in sorted(KNOWN_ADAPTERS):
        connection.execute("INSERT INTO source (adapter) VALUES (?)", (adapter_name,))
    for statement in _MIGRATION_1_INDEXES:
        connection.execute(statement)
    for statement in _MIGRATION_1_VIEWS:
        connection.execute(statement)
    for statement in _MIGRATION_1_TRIGGERS:
        connection.execute(statement)


# ---------------------------------------------------------------------------
# Migration 2: split `category_attribute` from holding the seed's
# parent-relative weight.
#
# Motivation: the column conflated two unrelated facts. For seed-known
# categories it held the seed's reference weight; for user-invented
# plan-leaves it held a placeholder `0`. The weighted-average code path
# that consumed it could not produce a per-plan answer when different
# plans weight a branch's children differently — there is no single
# "true" weight for a child that is independent of the plan that owns it.
# Plan weights now live exclusively on `plan_node`; this table is reduced
# to the intrinsic per-category fundamentals (`volatility`, `adjustment`).
#
# Effects on existing rows:
#   - Seed leaves had non-NULL vol/adj → carried forward unchanged.
#   - Seed branches had NULL vol/adj → dropped (their parent-relative
#     weight is no longer needed; nothing else on the row was meaningful).
#   - User-only plan-leaves with weight=0 and non-NULL vol/adj are
#     carried forward; the weight column simply disappears.
#
# No other table holds a foreign key into `category_attribute`, so the
# DROP/RENAME pattern works with `PRAGMA foreign_keys = ON`.
# ---------------------------------------------------------------------------

_MIGRATION_2_STATEMENTS: tuple[str, ...] = (
    # New shape: vol/adj are intrinsic, both NOT NULL. Row existence
    # encodes "this category has canonical fundamentals"; absence means
    # the category cannot be a plan-leaf until the walker fills it in.
    """
    CREATE TABLE category_attribute_new (
        category_id INTEGER PRIMARY KEY REFERENCES category(id) ON DELETE CASCADE,
        volatility_micros INTEGER NOT NULL CHECK (volatility_micros >= 0),
        adjustment_micros INTEGER NOT NULL CHECK (adjustment_micros >= 0)
    ) STRICT
    """,
    # Carry forward only the rows that actually carry vol/adj. Seed branches
    # had NULL on both and no longer have a reason to exist; the weighted-
    # average code path that consumed their weight is gone.
    """
    INSERT INTO category_attribute_new (category_id, volatility_micros, adjustment_micros)
    SELECT category_id, volatility_micros, adjustment_micros
    FROM category_attribute
    WHERE volatility_micros IS NOT NULL AND adjustment_micros IS NOT NULL
    """,
    "DROP TABLE category_attribute",
    "ALTER TABLE category_attribute_new RENAME TO category_attribute",
)


def _migration_2(connection: sqlite3.Connection) -> None:
    """Drop `weight_micros` from `category_attribute`; promote vol/adj to NOT NULL."""
    for statement in _MIGRATION_2_STATEMENTS:
        connection.execute(statement)


MIGRATIONS: List[Migration] = [
    _migration_1,
    _migration_2,
]


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply pending migrations in order. Idempotent and atomic per step.

    Reads `PRAGMA user_version` to find the latest applied version. Each
    migration runs inside its own explicit transaction so a failed
    migration rolls back cleanly. Refuses to start if the DB is newer
    than this binary supports (downgrade protection).
    """
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    target_version = len(MIGRATIONS)
    if current_version > target_version:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than this binary "
            f"supports (max {target_version}). Refusing to downgrade."
        )
    for index in range(current_version, target_version):
        version = index + 1
        connection.execute("BEGIN")
        try:
            MIGRATIONS[index](connection)
            connection.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _utc_now_iso()),
            )
            # `PRAGMA user_version = N` does not accept a bound parameter,
            # so the literal is interpolated. `version` is a controlled
            # integer from a known list, so there is no injection surface.
            connection.execute(f"PRAGMA user_version = {version}")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _utc_now_iso() -> str:
    """Return the current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.

    Stored in `schema_version.applied_at`. The CHECK constraint on that
    column requires the `Z` suffix, so we explicitly canonicalise the
    output (Python's default appends `+00:00`).
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")
