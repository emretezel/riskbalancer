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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List

# Each migration applies one numbered change. The list order is the schema
# version order: index `i` is version `i + 1`. The runner reads
# `PRAGMA user_version` to find where to resume and refuses to start if the
# DB is newer than this binary knows about (downgrade protection).
#
# `requires_fk_off=True` is for migrations that need the SQLite table-recreate
# pattern (drop a table that has incoming FK references). FK enforcement is
# toggled around the whole migration — the toggle has to happen outside any
# transaction because `PRAGMA foreign_keys` is a no-op inside one. After the
# body runs, the runner issues `PRAGMA foreign_key_check` to catch any
# dangling references the recreate may have left behind.


@dataclass(frozen=True)
class Migration:
    """One numbered DDL step plus the runtime knobs it needs.

    `func` receives an already-open `sqlite3.Connection` and applies the
    DDL for one schema version. `requires_fk_off` opts the migration into
    the FK-off envelope described above.
    """

    func: Callable[[sqlite3.Connection], None]
    requires_fk_off: bool = False


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
#
# IMPORTANT: this tuple is **append-only**. Migration 1 stamps a
# `source` row per entry (in sorted order, for stable surrogate ids).
# Reordering entries or removing one would either break the CHECK
# clause against existing rows or strand `instrument` / `account` rows
# whose `source_id` was assigned from the old ordering. A new broker
# is added by appending a new entry AND adding a migration that
# INSERTs the corresponding `source` row. A regression test pins the
# current ordered tuple so any accidental edit trips CI.
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


# ---------------------------------------------------------------------------
# Migration 3: defence-in-depth CHECK constraints across seven tables.
#
# Adds the following table-level CHECKs that SQLite cannot retrofit via
# `ALTER TABLE` on STRICT tables, so each affected table is recreated via
# the SQLite 12-step pattern (create-new, copy, drop-old, rename). FK
# enforcement is disabled around the whole step (see `requires_fk_off`
# on the Migration record below) because dropping a table with incoming
# FK references would otherwise abort.
#
#   category          parent_id != id    (self-cycle prevention)
#                     name = trim(name)   (no leading/trailing whitespace)
#   account           name = trim(name)
#   instrument        description trimmed and non-empty when not NULL
#   position          quantity_micro_units IS NULL OR >= 0   (long-only)
#                     description trimmed and non-empty when not NULL
#                     currency = upper(currency)
#   statement_import  statement_path trimmed and non-empty when not NULL
#   fx_rate           currency = upper(currency)
#   plan_node         parent_id != id    (self-cycle prevention)
#
# `position` is also recreated WITHOUT `idx_position_instrument` — that
# index has no caller in the current codebase (the cross-import "every
# position ever held in EMIM" query is documented but not yet written),
# so we let it die with the old table per CLAUDE.md's "justify each
# index by the query pattern it serves". `idx_mapping_instrument`
# (independently redundant with the UNIQUE autoindex) is dropped in a
# later migration; `mapping` is not recreated here.
#
# `category_path`, `current_import`, and `current_position` are dropped
# before the table teardown and recreated afterwards with their original
# definitions, because SQLite would leave them silently stale otherwise.
# ---------------------------------------------------------------------------

_MIGRATION_3_DROP_DEPENDENTS: tuple[str, ...] = (
    # Views first — current_position depends on current_import (a view),
    # so drop in this order.
    "DROP VIEW current_position",
    "DROP VIEW current_import",
    "DROP VIEW category_path",
    # The leaf-only mapping triggers reference `category` in their body.
    # SQLite refuses to drop a table referenced from a trigger body, so we
    # drop these triggers before recreating `category` and recreate them
    # with identical bodies once the dust settles.
    "DROP TRIGGER mapping_target_must_be_leaf_update",
    "DROP TRIGGER mapping_target_must_be_leaf_insert",
)

_MIGRATION_3_RECREATE_TABLES: tuple[str, ...] = (
    # category — parent_id self-cycle prevention + trim invariant on name.
    """
    CREATE TABLE category_new (
        id INTEGER PRIMARY KEY,
        parent_id INTEGER REFERENCES category(id) ON DELETE RESTRICT
            CHECK (parent_id IS NULL OR parent_id != id),
        name TEXT NOT NULL CHECK (length(name) > 0 AND name = trim(name)),
        UNIQUE (parent_id, name)
    ) STRICT
    """,
    """
    INSERT INTO category_new (id, parent_id, name)
    SELECT id, parent_id, name FROM category
    """,
    "DROP TABLE category",
    "ALTER TABLE category_new RENAME TO category",
    # account — name trim invariant.
    """
    CREATE TABLE account_new (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
        name TEXT NOT NULL CHECK (length(name) > 0 AND name = trim(name)),
        UNIQUE (user_id, source_id, name)
    ) STRICT
    """,
    """
    INSERT INTO account_new (id, user_id, source_id, name)
    SELECT id, user_id, source_id, name FROM account
    """,
    "DROP TABLE account",
    "ALTER TABLE account_new RENAME TO account",
    # instrument — description must be NULL or trimmed+non-empty.
    """
    CREATE TABLE instrument_new (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
        instrument_id_text TEXT NOT NULL CHECK (length(instrument_id_text) > 0),
        description TEXT CHECK (
            description IS NULL
            OR (length(description) > 0 AND description = trim(description))
        ),
        UNIQUE (source_id, instrument_id_text)
    ) STRICT
    """,
    """
    INSERT INTO instrument_new (id, source_id, instrument_id_text, description)
    SELECT id, source_id, instrument_id_text, description FROM instrument
    """,
    "DROP TABLE instrument",
    "ALTER TABLE instrument_new RENAME TO instrument",
    # statement_import — statement_path trim+non-empty when present.
    f"""
    CREATE TABLE statement_import_new (
        id INTEGER PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
        as_of TEXT NOT NULL CHECK (as_of GLOB '{_DATE_GLOB}'),
        statement_path TEXT CHECK (
            statement_path IS NULL
            OR (length(statement_path) > 0 AND statement_path = trim(statement_path))
        ),
        imported_at TEXT NOT NULL CHECK (imported_at GLOB '{_TIMESTAMP_GLOB}'),
        UNIQUE (account_id, as_of)
    ) STRICT
    """,
    """
    INSERT INTO statement_import_new (id, account_id, as_of, statement_path, imported_at)
    SELECT id, account_id, as_of, statement_path, imported_at FROM statement_import
    """,
    "DROP TABLE statement_import",
    "ALTER TABLE statement_import_new RENAME TO statement_import",
    # position — long-only quantity, trimmed description, uppercase currency.
    # The `idx_position_instrument` index is intentionally NOT recreated.
    """
    CREATE TABLE position_new (
        id INTEGER PRIMARY KEY,
        statement_import_id INTEGER NOT NULL
            REFERENCES statement_import(id) ON DELETE CASCADE,
        instrument_id INTEGER NOT NULL REFERENCES instrument(id) ON DELETE RESTRICT,
        description TEXT CHECK (
            description IS NULL
            OR (length(description) > 0 AND description = trim(description))
        ),
        quantity_micro_units INTEGER
            CHECK (quantity_micro_units IS NULL OR quantity_micro_units >= 0),
        market_value_native_decithou INTEGER NOT NULL
            CHECK (market_value_native_decithou >= 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
        UNIQUE (statement_import_id, instrument_id)
    ) STRICT
    """,
    """
    INSERT INTO position_new (
        id, statement_import_id, instrument_id, description,
        quantity_micro_units, market_value_native_decithou, currency
    )
    SELECT id, statement_import_id, instrument_id, description,
           quantity_micro_units, market_value_native_decithou, currency
    FROM position
    """,
    "DROP TABLE position",
    "ALTER TABLE position_new RENAME TO position",
    # fx_rate — uppercase currency invariant.
    f"""
    CREATE TABLE fx_rate_new (
        id INTEGER PRIMARY KEY,
        rate_date TEXT NOT NULL CHECK (rate_date GLOB '{_DATE_GLOB}'),
        currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
        gbp_rate_micros INTEGER NOT NULL CHECK (gbp_rate_micros > 0),
        UNIQUE (rate_date, currency)
    ) STRICT
    """,
    """
    INSERT INTO fx_rate_new (id, rate_date, currency, gbp_rate_micros)
    SELECT id, rate_date, currency, gbp_rate_micros FROM fx_rate
    """,
    "DROP TABLE fx_rate",
    "ALTER TABLE fx_rate_new RENAME TO fx_rate",
    # plan_node — self-cycle prevention.
    """
    CREATE TABLE plan_node_new (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES plan_node(id) ON DELETE CASCADE
            CHECK (parent_id IS NULL OR parent_id != id),
        category_id INTEGER NOT NULL REFERENCES category(id) ON DELETE RESTRICT,
        weight_micros INTEGER NOT NULL
            CHECK (weight_micros >= 0 AND weight_micros <= 1000000),
        UNIQUE (user_id, parent_id, category_id)
    ) STRICT
    """,
    """
    INSERT INTO plan_node_new (id, user_id, parent_id, category_id, weight_micros)
    SELECT id, user_id, parent_id, category_id, weight_micros FROM plan_node
    """,
    "DROP TABLE plan_node",
    "ALTER TABLE plan_node_new RENAME TO plan_node",
)

_MIGRATION_3_RECREATE_INDEXES: tuple[str, ...] = (
    # Recreate the partial unique index that closed SQLite's NULL-distinct
    # gap on top-level category names. The auto-indexes from UNIQUE
    # constraints come back automatically with each new table; only this
    # explicit one needs reinstating.
    "CREATE UNIQUE INDEX idx_category_top_level_name ON category(name) WHERE parent_id IS NULL",
)

# Views and triggers are dropped before the table teardown and recreated
# with their original definitions afterwards. The definitions match
# migration 1 exactly; any change to them is the job of a later
# migration, not this one.
_MIGRATION_3_RECREATE_VIEWS: tuple[str, ...] = _MIGRATION_1_VIEWS
_MIGRATION_3_RECREATE_TRIGGERS: tuple[str, ...] = _MIGRATION_1_TRIGGERS


def _migration_3(connection: sqlite3.Connection) -> None:
    """Recreate seven tables to add defence-in-depth CHECK constraints."""
    for statement in _MIGRATION_3_DROP_DEPENDENTS:
        connection.execute(statement)
    for statement in _MIGRATION_3_RECREATE_TABLES:
        connection.execute(statement)
    for statement in _MIGRATION_3_RECREATE_INDEXES:
        connection.execute(statement)
    for statement in _MIGRATION_3_RECREATE_VIEWS:
        connection.execute(statement)
    for statement in _MIGRATION_3_RECREATE_TRIGGERS:
        connection.execute(statement)


# ---------------------------------------------------------------------------
# Migration 4: cross-row invariant triggers, redundant-index cleanup, and a
# depth-capped `category_path` view.
#
# Adds:
#   - `category_no_cycle_update` / `plan_node_no_cycle_update`:
#       BEFORE UPDATE OF parent_id triggers using a recursive CTE with a
#       depth cap of 32 to detect multi-row cycles. The single-row case
#       (parent_id = id) is already blocked by a CHECK from migration 3.
#       INSERT cannot create a cycle (the new row's id is not referenced
#       by anything yet), so these triggers fire on UPDATE only.
#   - `plan_node_parent_same_user_*`:
#       A plan_node's parent must belong to the same user_id as the
#       node itself — otherwise a buggy write path could splice one
#       user's plan into another's. Fires on INSERT and on UPDATE OF
#       user_id, parent_id.
#   - `plan_node_parent_category_lineage_*`:
#       The plan tree mirrors the category tree's parent/child lineage:
#       a root plan_node references a root category, and a non-root
#       plan_node's category is a child (in the global category tree)
#       of its parent plan_node's category. Fires on INSERT and on
#       UPDATE OF parent_id, category_id.
#   - `position_instrument_source_matches_*`:
#       A position's instrument must come from the same broker (source)
#       as the statement_import's account. Catches the worst-case
#       mis-attribution where an IBKR instrument lands on an AJ Bell
#       statement_import. Fires on INSERT and on UPDATE OF instrument_id,
#       statement_import_id.
#
# Drops:
#   - `idx_mapping_instrument`: redundant with the autoindex over
#     `UNIQUE (instrument_id, category_id)` whose leading column already
#     services `WHERE instrument_id = ?` lookups (verified via
#     `EXPLAIN QUERY PLAN`). See CLAUDE.md "Avoid redundant indexes".
#
# Replaces:
#   - `category_path`: same recursive structure, but the CTE carries an
#     explicit `depth` column and caps recursion at 32 levels. If a
#     malformed parent chain ever creates a cycle (the cycle triggers
#     above are the primary guard, this is defence in depth), the view
#     terminates rather than looping until SQLite exhausts memory.
# ---------------------------------------------------------------------------

_MIGRATION_4_DROP_BEFORE: tuple[str, ...] = (
    # idx_mapping_instrument first — the autoindex already covers the
    # query pattern.
    "DROP INDEX idx_mapping_instrument",
    # Replace category_path with the depth-capped version. SQLite has no
    # CREATE OR REPLACE VIEW, so drop+create.
    "DROP VIEW category_path",
)

_MIGRATION_4_CREATE_VIEW: str = """
CREATE VIEW category_path AS
WITH RECURSIVE walk(id, parent_id, path, depth) AS (
    SELECT id, parent_id, name, 1
    FROM category
    WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, c.parent_id, walk.path || ' / ' || c.name, walk.depth + 1
    FROM category c
    JOIN walk ON c.parent_id = walk.id
    WHERE walk.depth < 32
)
SELECT id, path FROM walk
"""

_MIGRATION_4_TRIGGERS: tuple[str, ...] = (
    # ---- Cycle prevention ------------------------------------------------
    """
    CREATE TRIGGER category_no_cycle_update
    BEFORE UPDATE OF parent_id ON category
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'category parent_id would create a cycle')
        WHERE EXISTS (
            WITH RECURSIVE ancestors(id, depth) AS (
                SELECT NEW.parent_id, 1
                UNION ALL
                SELECT c.parent_id, a.depth + 1
                FROM category c
                JOIN ancestors a ON c.id = a.id
                WHERE c.parent_id IS NOT NULL AND a.depth < 32
            )
            SELECT 1 FROM ancestors WHERE id = NEW.id
        );
    END
    """,
    """
    CREATE TRIGGER plan_node_no_cycle_update
    BEFORE UPDATE OF parent_id ON plan_node
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan_node parent_id would create a cycle')
        WHERE EXISTS (
            WITH RECURSIVE ancestors(id, depth) AS (
                SELECT NEW.parent_id, 1
                UNION ALL
                SELECT pn.parent_id, a.depth + 1
                FROM plan_node pn
                JOIN ancestors a ON pn.id = a.id
                WHERE pn.parent_id IS NOT NULL AND a.depth < 32
            )
            SELECT 1 FROM ancestors WHERE id = NEW.id
        );
    END
    """,
    # ---- plan_node parent must belong to same user ----------------------
    """
    CREATE TRIGGER plan_node_parent_same_user_insert
    BEFORE INSERT ON plan_node
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan_node parent must belong to the same user')
        WHERE NEW.user_id != (SELECT user_id FROM plan_node WHERE id = NEW.parent_id);
    END
    """,
    """
    CREATE TRIGGER plan_node_parent_same_user_update
    BEFORE UPDATE OF user_id, parent_id ON plan_node
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'plan_node parent must belong to the same user')
        WHERE NEW.user_id != (SELECT user_id FROM plan_node WHERE id = NEW.parent_id);
    END
    """,
    # ---- plan_node category lineage mirrors category tree ---------------
    # The two cases are combined into one WHERE so the trigger covers both
    # root and non-root cases. `IS NOT` is used instead of `!=` because
    # `category.parent_id` is nullable and `!=` against NULL is NULL (not
    # TRUE), which would silently let mismatches through.
    """
    CREATE TRIGGER plan_node_parent_category_lineage_insert
    BEFORE INSERT ON plan_node
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'plan_node category lineage must mirror the category tree')
        WHERE
            (NEW.parent_id IS NULL
                AND (SELECT parent_id FROM category WHERE id = NEW.category_id) IS NOT NULL)
            OR (NEW.parent_id IS NOT NULL
                AND (SELECT category_id FROM plan_node WHERE id = NEW.parent_id)
                    IS NOT (SELECT parent_id FROM category WHERE id = NEW.category_id));
    END
    """,
    """
    CREATE TRIGGER plan_node_parent_category_lineage_update
    BEFORE UPDATE OF parent_id, category_id ON plan_node
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'plan_node category lineage must mirror the category tree')
        WHERE
            (NEW.parent_id IS NULL
                AND (SELECT parent_id FROM category WHERE id = NEW.category_id) IS NOT NULL)
            OR (NEW.parent_id IS NOT NULL
                AND (SELECT category_id FROM plan_node WHERE id = NEW.parent_id)
                    IS NOT (SELECT parent_id FROM category WHERE id = NEW.category_id));
    END
    """,
    # ---- position instrument source must match account source -----------
    """
    CREATE TRIGGER position_instrument_source_matches_insert
    BEFORE INSERT ON position
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'position instrument must come from the same broker as the statement')
        WHERE (SELECT source_id FROM instrument WHERE id = NEW.instrument_id)
            != (SELECT a.source_id
                FROM statement_import si
                JOIN account a ON a.id = si.account_id
                WHERE si.id = NEW.statement_import_id);
    END
    """,
    """
    CREATE TRIGGER position_instrument_source_matches_update
    BEFORE UPDATE OF instrument_id, statement_import_id ON position
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'position instrument must come from the same broker as the statement')
        WHERE (SELECT source_id FROM instrument WHERE id = NEW.instrument_id)
            != (SELECT a.source_id
                FROM statement_import si
                JOIN account a ON a.id = si.account_id
                WHERE si.id = NEW.statement_import_id);
    END
    """,
)


def _migration_4(connection: sqlite3.Connection) -> None:
    """Add cross-row triggers, drop the redundant mapping index, cap the path view."""
    for statement in _MIGRATION_4_DROP_BEFORE:
        connection.execute(statement)
    connection.execute(_MIGRATION_4_CREATE_VIEW)
    for statement in _MIGRATION_4_TRIGGERS:
        connection.execute(statement)


MIGRATIONS: List[Migration] = [
    Migration(_migration_1),
    Migration(_migration_2),
    Migration(_migration_3, requires_fk_off=True),
    Migration(_migration_4),
]


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply pending migrations in order. Idempotent and atomic per step.

    Reads `PRAGMA user_version` to find the latest applied version. Each
    migration runs inside its own explicit transaction so a failed
    migration rolls back cleanly. Refuses to start if the DB is newer
    than this binary supports (downgrade protection).

    Migrations declaring `requires_fk_off=True` are wrapped in an
    explicit `PRAGMA foreign_keys = OFF` / `ON` envelope (toggled outside
    the transaction, since the pragma is a no-op inside one). After the
    body runs, `PRAGMA foreign_key_check` validates that nothing left
    dangling — if it did, the migration aborts and rolls back rather
    than committing a broken graph. The envelope's restore-to-ON happens
    in a `finally` block so a failure inside the transaction can't leave
    the connection with FK off.
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
        migration = MIGRATIONS[index]
        # FK toggling must happen outside any transaction. The runner is
        # the only piece of code that issues these PRAGMAs from inside
        # the migration loop, so each step opens and closes its own FK
        # envelope cleanly without affecting later steps.
        if migration.requires_fk_off:
            connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN")
            try:
                migration.func(connection)
                if migration.requires_fk_off:
                    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                    if violations:
                        raise RuntimeError(
                            f"Migration {version} left {len(violations)} dangling "
                            f"foreign key reference(s): {violations!r}"
                        )
                connection.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now_iso()),
                )
                # `PRAGMA user_version = N` does not accept a bound
                # parameter, so the literal is interpolated. `version`
                # is a controlled integer from a known list, so there is
                # no injection surface.
                connection.execute(f"PRAGMA user_version = {version}")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            # Restore FK enforcement regardless of success or failure so
            # the connection never escapes this function with FK off.
            if migration.requires_fk_off:
                connection.execute("PRAGMA foreign_keys = ON")


def _utc_now_iso() -> str:
    """Return the current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.

    Stored in `schema_version.applied_at`. The CHECK constraint on that
    column requires the `Z` suffix, so we explicitly canonicalise the
    output (Python's default appends `+00:00`).
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")
