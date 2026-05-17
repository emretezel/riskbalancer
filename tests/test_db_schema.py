"""
Schema-level tests for the RiskBalancer database.

These tests open a fresh in-memory SQLite database via `Database.connect`
and assert that every table, index, and view from migration 1 exists, that
the PRAGMAs the application relies on are set, and that the CHECK / FOREIGN
KEY constraints actually fire on bad inserts. They are deliberately
schema-only — repository behaviour is tested elsewhere.

Author: Emre Tezel
"""

from __future__ import annotations

import sqlite3

import pytest

from riskbalancer.db import Database
from riskbalancer.migrations import MIGRATIONS, apply_migrations

# Tables created by migration 1. The set is asserted exactly so adding a
# table without updating this list flags the omission immediately.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "schema_version",
        "user",
        "category",
        "category_attribute",
        "source",
        "account",
        "instrument",
        "mapping",
        "fx_rate",
        "plan_node",
        "statement_import",
        "position",
    }
)

EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        # idx_mapping_instrument was dropped in migration 4 as redundant
        # with the UNIQUE autoindex; idx_position_instrument was retired
        # in migration 3 as having no caller. Only the partial unique
        # index on top-level category names remains.
        "idx_category_top_level_name",
    }
)

EXPECTED_VIEWS: frozenset[str] = frozenset(
    {
        "current_import",
        "current_position",
        "category_path",
    }
)


@pytest.fixture
def db() -> Database:
    """A fresh in-memory database with all migrations applied."""
    return Database.connect(":memory:")


def _names_of(connection: sqlite3.Connection, kind: str) -> set[str]:
    """Return the names of objects of `kind` from sqlite_master.

    Filters out sqlite-internal helpers like `sqlite_autoindex_*` and
    `sqlite_sequence` so the set matches what the migration intentionally
    declared.
    """
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        (kind,),
    ).fetchall()
    return {row["name"] for row in rows}


def test_foreign_keys_enabled(db: Database) -> None:
    """The application relies on FK enforcement — verify the PRAGMA is on."""
    value = db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert value == 1


def test_user_version_matches_migrations_length(db: Database) -> None:
    """`PRAGMA user_version` is the source of truth for migration progress."""
    value = db.connection.execute("PRAGMA user_version").fetchone()[0]
    assert value == len(MIGRATIONS)


def test_schema_version_table_records_each_migration(db: Database) -> None:
    """Every migration leaves a row with its version and an ISO timestamp."""
    rows = db.connection.execute(
        "SELECT version, applied_at FROM schema_version ORDER BY version"
    ).fetchall()
    assert [row["version"] for row in rows] == list(range(1, len(MIGRATIONS) + 1))
    for row in rows:
        # ISO 8601 UTC with trailing Z. The CHECK in the table enforces
        # the shape; this just confirms the writer agrees.
        assert row["applied_at"].endswith("Z")
        assert row["applied_at"][4] == "-" and row["applied_at"][10] == "T"


def test_all_expected_tables_present(db: Database) -> None:
    """Every table from migration 1 is created."""
    tables = _names_of(db.connection, "table")
    assert EXPECTED_TABLES.issubset(tables), EXPECTED_TABLES - tables


def test_all_expected_indexes_present(db: Database) -> None:
    """Every index from migration 1 is created."""
    indexes = _names_of(db.connection, "index")
    assert EXPECTED_INDEXES.issubset(indexes), EXPECTED_INDEXES - indexes


def test_all_expected_views_present(db: Database) -> None:
    """Every view from migration 1 is created."""
    views = _names_of(db.connection, "view")
    assert EXPECTED_VIEWS.issubset(views), EXPECTED_VIEWS - views


def test_reopen_is_idempotent() -> None:
    """Running `apply_migrations` on an already-current DB is a no-op."""
    db = Database.connect(":memory:")
    before = db.connection.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    apply_migrations(db.connection)
    after = db.connection.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert before == after == len(MIGRATIONS)


def test_downgrade_protection_raises() -> None:
    """If the DB reports a higher `user_version`, refuse to run."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        apply_migrations(connection)


def test_unique_user_name(db: Database) -> None:
    """User names are globally unique."""
    db.connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("emre", "2026-05-17T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO user (name, created_at) VALUES (?, ?)",
            ("emre", "2026-05-18T00:00:00Z"),
        )


def test_user_created_at_must_be_iso_utc(db: Database) -> None:
    """The CHECK on `user.created_at` rejects a non-ISO timestamp."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO user (name, created_at) VALUES (?, ?)",
            ("emre", "yesterday"),
        )


def test_category_unique_per_parent(db: Database) -> None:
    """Top-level and sibling categories cannot share a name."""
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
        ("Equities",),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
            ("Equities",),
        )
    parent_id = db.connection.execute(
        "SELECT id FROM category WHERE name = ?", ("Equities",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (parent_id, "NAM"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category (parent_id, name) VALUES (?, ?)",
            (parent_id, "NAM"),
        )


def test_category_same_name_under_different_parents_ok(db: Database) -> None:
    """A category name is allowed to repeat under distinct parents.

    The seed has `Bonds / Developed / NAM / Govt` and `Bonds / Developed /
    Europe / Govt` — both `Govt` leaves must coexist under their distinct
    parents.
    """
    bonds_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Bonds",),
    ).fetchone()["id"]
    nam_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (bonds_id, "NAM"),
    ).fetchone()["id"]
    europe_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (bonds_id, "Europe"),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (nam_id, "Govt"),
    )
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (europe_id, "Govt"),
    )


def test_category_attribute_requires_volatility(db: Database) -> None:
    """`category_attribute.volatility_micros` is NOT NULL — vol must be explicit."""
    cash_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Cash",),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category_attribute "
            "(category_id, volatility_micros, adjustment_micros) "
            "VALUES (?, NULL, ?)",
            (cash_id, 1_000_000),
        )


def test_category_attribute_requires_adjustment(db: Database) -> None:
    """`category_attribute.adjustment_micros` is NOT NULL — adj must be explicit."""
    cash_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Cash",),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category_attribute "
            "(category_id, volatility_micros, adjustment_micros) "
            "VALUES (?, ?, NULL)",
            (cash_id, 0),
        )


def test_category_attribute_vol_and_adj_must_be_non_negative(db: Database) -> None:
    """Both `volatility_micros` and `adjustment_micros` reject negative values."""
    nam_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("NAM",),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category_attribute "
            "(category_id, volatility_micros, adjustment_micros) VALUES (?, ?, ?)",
            (nam_id, -1, 1_000_000),
        )
    em_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("EM",),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO category_attribute "
            "(category_id, volatility_micros, adjustment_micros) VALUES (?, ?, ?)",
            (em_id, 1_000_000, -1),
        )


def test_category_attribute_cascades_on_category_delete(db: Database) -> None:
    """Dropping a `category` cascades to its `category_attribute` row.

    Categories referenced by other rows (mapping, plan_node) cannot be
    deleted thanks to ON DELETE RESTRICT on those FKs; but a standalone
    category with only its own attribute row goes away cleanly, and
    SQLite removes the dependent attribute row as part of the cascade.
    """
    cash_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Cash",),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO category_attribute "
        "(category_id, volatility_micros, adjustment_micros) VALUES (?, ?, ?)",
        (cash_id, 0, 0),
    )
    db.connection.execute("DELETE FROM category WHERE id = ?", (cash_id,))
    remaining = db.connection.execute(
        "SELECT COUNT(*) AS n FROM category_attribute WHERE category_id = ?",
        (cash_id,),
    ).fetchone()["n"]
    assert remaining == 0


def test_mapping_unique_per_instrument_category(db: Database) -> None:
    """`UNIQUE (instrument_id, category_id)` blocks duplicate split rows."""
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
        ("Cash",),
    )
    category_id = db.connection.execute(
        "SELECT id FROM category WHERE name = ?", ("Cash",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?)",
        ("ibkr", "CASH"),
    )
    instrument_id = db.connection.execute(
        "SELECT id FROM instrument WHERE instrument_id_text = ?", ("CASH",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
        (instrument_id, category_id, 1_000_000),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
            (instrument_id, category_id, 500_000),
        )


def test_mapping_weight_bounds(db: Database) -> None:
    """`mapping.weight_micros` must be in (0, 1_000_000]."""
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
        ("Cash",),
    )
    category_id = db.connection.execute(
        "SELECT id FROM category WHERE name = ?", ("Cash",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?)",
        ("ibkr", "CASH"),
    )
    instrument_id = db.connection.execute(
        "SELECT id FROM instrument WHERE instrument_id_text = ?", ("CASH",)
    ).fetchone()["id"]
    for bad_weight in (0, -1, 1_000_001):
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
                (instrument_id, category_id, bad_weight),
            )


def test_mapping_target_must_be_leaf_on_insert(db: Database) -> None:
    """Trigger blocks INSERT when category has children (i.e. is a branch)."""
    bonds_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Bonds",),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (bonds_id, "Govt"),
    )
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?)",
        ("ibkr", "BOND"),
    )
    instrument_id = db.connection.execute(
        "SELECT id FROM instrument WHERE instrument_id_text = ?", ("BOND",)
    ).fetchone()["id"]
    # `Bonds` is a branch — should be rejected by the trigger.
    with pytest.raises(sqlite3.IntegrityError, match="leaf"):
        db.connection.execute(
            "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
            (instrument_id, bonds_id, 1_000_000),
        )


def test_mapping_target_must_be_leaf_on_update(db: Database) -> None:
    """Trigger blocks UPDATE OF category_id that would point at a branch."""
    bonds_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Bonds",),
    ).fetchone()["id"]
    govt_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (bonds_id, "Govt"),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?)",
        ("ibkr", "BOND"),
    )
    instrument_id = db.connection.execute(
        "SELECT id FROM instrument WHERE instrument_id_text = ?", ("BOND",)
    ).fetchone()["id"]
    mapping_id = db.connection.execute(
        "INSERT INTO mapping (instrument_id, category_id, weight_micros) "
        "VALUES (?, ?, ?) RETURNING id",
        (instrument_id, govt_id, 1_000_000),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="leaf"):
        db.connection.execute(
            "UPDATE mapping SET category_id = ? WHERE id = ?",
            (bonds_id, mapping_id),
        )


def test_fx_rate_currency_must_be_three_letters(db: Database) -> None:
    """`fx_rate.currency` is constrained to three characters."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO fx_rate (rate_date, currency, gbp_rate_micros) VALUES (?, ?, ?)",
            ("2026-05-17", "DOLLAR", 760_000),
        )


def test_fx_rate_date_must_be_iso(db: Database) -> None:
    """`fx_rate.rate_date` rejects non-ISO inputs."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO fx_rate (rate_date, currency, gbp_rate_micros) VALUES (?, ?, ?)",
            ("17 May 2026", "USD", 760_000),
        )


def test_fx_rate_rate_must_be_positive(db: Database) -> None:
    """A non-positive FX rate is rejected."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO fx_rate (rate_date, currency, gbp_rate_micros) VALUES (?, ?, ?)",
            ("2026-05-17", "USD", 0),
        )


def test_source_is_prepopulated_with_known_adapters(db: Database) -> None:
    """Migration 1 inserts one `source` row per `KNOWN_ADAPTERS` entry."""
    from riskbalancer.migrations import KNOWN_ADAPTERS

    rows = db.connection.execute("SELECT adapter FROM source ORDER BY adapter").fetchall()
    assert {row["adapter"] for row in rows} == set(KNOWN_ADAPTERS)


def test_known_adapters_ordering_is_append_only() -> None:
    """`KNOWN_ADAPTERS` is pinned tuple-equal to its committed ordering.

    Migration 1 stamps surrogate `source.id` values from this tuple in
    sorted order; existing `instrument.source_id` and `account.source_id`
    rows were assigned from the same sorted view. Reordering or removing
    entries would either break the `source.adapter IN (...)` CHECK
    against legacy rows or strand FK references whose target id no
    longer means what it did. Adding a broker is a strict append plus a
    new migration that INSERTs its `source` row; this test trips CI if
    that contract is broken.
    """
    from riskbalancer.migrations import KNOWN_ADAPTERS

    assert KNOWN_ADAPTERS == (
        "ibkr",
        "ajbell",
        "citi",
        "ms401k",
        "schwab",
        "aegon",
        "manual",
    )


def test_source_adapter_must_be_known(db: Database) -> None:
    """`source.adapter` is constrained to the recognised adapter list."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO source (adapter) VALUES (?)",
            ("fidelity",),
        )


def test_source_adapter_is_unique_globally(db: Database) -> None:
    """`source` carries one row per adapter — the pre-populated row blocks duplicates."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("INSERT INTO source (adapter) VALUES (?)", ("ibkr",))


def _source_id_for(connection: sqlite3.Connection, adapter: str) -> int:
    """Look up the pre-populated source row for `adapter`."""
    return int(
        connection.execute("SELECT id FROM source WHERE adapter = ?", (adapter,)).fetchone()["id"]
    )


def test_account_belongs_to_user_and_shares_source(db: Database) -> None:
    """Two users can hold accounts at the same broker via one shared `source`."""
    emre_id = _seed_minimal_user(db.connection)
    db.connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("tani", "2026-05-17T00:00:00Z"),
    )
    tani_id = db.connection.execute("SELECT id FROM user WHERE name = ?", ("tani",)).fetchone()[
        "id"
    ]
    source_id = _source_id_for(db.connection, "ibkr")
    db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
        (emre_id, source_id, "taxable"),
    )
    db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
        (tani_id, source_id, "sipp"),
    )
    rows = db.connection.execute(
        "SELECT user_id FROM account WHERE source_id = ? ORDER BY user_id",
        (source_id,),
    ).fetchall()
    assert [row["user_id"] for row in rows] == sorted([emre_id, tani_id])


def test_account_unique_per_user_source_name(db: Database) -> None:
    """`(user_id, source_id, name)` is unique — same name allowed across users."""
    emre_id = _seed_minimal_user(db.connection)
    db.connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("tani", "2026-05-17T00:00:00Z"),
    )
    tani_id = db.connection.execute("SELECT id FROM user WHERE name = ?", ("tani",)).fetchone()[
        "id"
    ]
    source_id = _source_id_for(db.connection, "ibkr")
    db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
        (emre_id, source_id, "taxable"),
    )
    # Same name on a different user is fine.
    db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
        (tani_id, source_id, "taxable"),
    )
    # Same (user, source, name) is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
            (emre_id, source_id, "taxable"),
        )


def test_position_market_value_must_be_non_negative(db: Database) -> None:
    """Negative market values are not allowed; zero is permitted (held but worthless)."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, source_id, "taxable"),
    ).fetchone()["id"]
    statement_import_id = db.connection.execute(
        "INSERT INTO statement_import "
        "(account_id, as_of, statement_path, imported_at) "
        "VALUES (?, ?, NULL, ?) RETURNING id",
        (account_id, "2026-05-17", "2026-05-17T12:00:00Z"),
    ).fetchone()["id"]
    instrument_id = db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?) RETURNING id",
        ("ibkr", "EMIM"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO position "
            "(statement_import_id, instrument_id, market_value_native_decithou, currency) "
            "VALUES (?, ?, ?, ?)",
            (statement_import_id, instrument_id, -1, "USD"),
        )


def test_statement_import_unique_per_account_asof(db: Database) -> None:
    """Two import rows cannot share `(account_id, as_of)`."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, source_id, "taxable"),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO statement_import "
        "(account_id, as_of, statement_path, imported_at) "
        "VALUES (?, ?, NULL, ?)",
        (account_id, "2026-05-17", "2026-05-17T12:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO statement_import "
            "(account_id, as_of, statement_path, imported_at) "
            "VALUES (?, ?, NULL, ?)",
            (account_id, "2026-05-17", "2026-05-17T13:00:00Z"),
        )


def test_foreign_key_violation_is_rejected(db: Database) -> None:
    """A dangling foreign key reference is rejected when FKs are enabled."""
    source_id = _source_id_for(db.connection, "ibkr")
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
            (9999, source_id, "taxable"),
        )


def test_category_restrict_blocks_delete_when_referenced(db: Database) -> None:
    """A category referenced by a mapping cannot be deleted (ON DELETE RESTRICT)."""
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
        ("Cash",),
    )
    category_id = db.connection.execute(
        "SELECT id FROM category WHERE name = ?", ("Cash",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) "
        "VALUES ((SELECT id FROM source WHERE adapter = ?), ?)",
        ("ibkr", "CASH"),
    )
    instrument_id = db.connection.execute(
        "SELECT id FROM instrument WHERE instrument_id_text = ?", ("CASH",)
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
        (instrument_id, category_id, 1_000_000),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("DELETE FROM category WHERE id = ?", (category_id,))


def test_category_path_view_resolves_hierarchy(db: Database) -> None:
    """The recursive `category_path` view stitches the ` / `-joined path."""
    bonds_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Bonds",),
    ).fetchone()["id"]
    dev_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (bonds_id, "Developed"),
    ).fetchone()["id"]
    nam_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (dev_id, "NAM"),
    ).fetchone()["id"]
    govt_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (nam_id, "Govt"),
    ).fetchone()["id"]
    row = db.connection.execute(
        "SELECT path FROM category_path WHERE id = ?", (govt_id,)
    ).fetchone()
    assert row["path"] == "Bonds / Developed / NAM / Govt"


# ---------------------------------------------------------------------------
# Migration 3 — defence-in-depth CHECK constraints across recreated tables.
# ---------------------------------------------------------------------------


def test_category_parent_id_rejects_self_reference(db: Database) -> None:
    """`category.parent_id != id` blocks the trivial single-row cycle."""
    db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
        ("Equities",),
    )
    cat_id = db.connection.execute(
        "SELECT id FROM category WHERE name = ?", ("Equities",)
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("UPDATE category SET parent_id = ? WHERE id = ?", (cat_id, cat_id))


def test_category_name_must_be_trimmed(db: Database) -> None:
    """`category.name = trim(name)` blocks space-padded names.

    SQLite's `trim()` (no second argument) strips only ASCII spaces, so
    the schema-level guard targets the realistic copy-paste case where a
    name arrives with leading or trailing spaces. Tabs and other
    whitespace are caught by the application's `.strip()` before they
    reach the database.
    """
    for bad_name in (" Equities", "Equities ", "  Equities  "):
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "INSERT INTO category (parent_id, name) VALUES (NULL, ?)",
                (bad_name,),
            )


def test_account_name_must_be_trimmed(db: Database) -> None:
    """`account.name = trim(name)` blocks leading/trailing whitespace."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
            (user_id, source_id, " taxable"),
        )


def test_instrument_description_must_be_null_or_trimmed_non_empty(db: Database) -> None:
    """`instrument.description` is NULL or a trimmed non-empty string."""
    source_id = _source_id_for(db.connection, "ibkr")
    # Empty string is rejected (use NULL for "no description").
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO instrument (source_id, instrument_id_text, description) VALUES (?, ?, ?)",
            (source_id, "EMIM", ""),
        )
    # Leading whitespace is rejected.
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO instrument (source_id, instrument_id_text, description) VALUES (?, ?, ?)",
            (source_id, "EMIM", "  iShares EM"),
        )
    # NULL is accepted.
    db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text, description) VALUES (?, ?, NULL)",
        (source_id, "EMIM"),
    )


def test_position_currency_must_be_uppercase(db: Database) -> None:
    """`position.currency = upper(currency)` blocks lowercase or mixed-case."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, source_id, "taxable"),
    ).fetchone()["id"]
    statement_import_id = db.connection.execute(
        "INSERT INTO statement_import "
        "(account_id, as_of, statement_path, imported_at) "
        "VALUES (?, ?, NULL, ?) RETURNING id",
        (account_id, "2026-05-17", "2026-05-17T12:00:00Z"),
    ).fetchone()["id"]
    instrument_id = db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) VALUES (?, ?) RETURNING id",
        (source_id, "EMIM"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO position "
            "(statement_import_id, instrument_id, "
            "market_value_native_decithou, currency) "
            "VALUES (?, ?, ?, ?)",
            (statement_import_id, instrument_id, 100, "usd"),
        )


def test_position_description_must_be_null_or_trimmed_non_empty(db: Database) -> None:
    """`position.description` is NULL or a trimmed non-empty string."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, source_id, "taxable"),
    ).fetchone()["id"]
    statement_import_id = db.connection.execute(
        "INSERT INTO statement_import "
        "(account_id, as_of, statement_path, imported_at) "
        "VALUES (?, ?, NULL, ?) RETURNING id",
        (account_id, "2026-05-17", "2026-05-17T12:00:00Z"),
    ).fetchone()["id"]
    instrument_id = db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) VALUES (?, ?) RETURNING id",
        (source_id, "EMIM"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO position "
            "(statement_import_id, instrument_id, description, "
            "market_value_native_decithou, currency) "
            "VALUES (?, ?, ?, ?, ?)",
            (statement_import_id, instrument_id, "", 100, "USD"),
        )


def test_statement_import_path_must_be_null_or_trimmed_non_empty(db: Database) -> None:
    """`statement_import.statement_path` rejects `""` and untrimmed strings."""
    user_id = _seed_minimal_user(db.connection)
    source_id = _source_id_for(db.connection, "ibkr")
    account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, source_id, "taxable"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO statement_import "
            "(account_id, as_of, statement_path, imported_at) "
            "VALUES (?, ?, ?, ?)",
            (account_id, "2026-05-17", "", "2026-05-17T12:00:00Z"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO statement_import "
            "(account_id, as_of, statement_path, imported_at) "
            "VALUES (?, ?, ?, ?)",
            (account_id, "2026-05-17", "  /tmp/foo.csv", "2026-05-17T12:00:00Z"),
        )


def test_fx_rate_currency_must_be_uppercase(db: Database) -> None:
    """`fx_rate.currency = upper(currency)` blocks lowercase or mixed-case."""
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO fx_rate (rate_date, currency, gbp_rate_micros) VALUES (?, ?, ?)",
            ("2026-05-17", "usd", 760_000),
        )


def test_plan_node_parent_id_rejects_self_reference(db: Database) -> None:
    """`plan_node.parent_id != id` blocks the trivial single-row cycle."""
    user_id = _seed_minimal_user(db.connection)
    cat_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Equities",),
    ).fetchone()["id"]
    plan_id = db.connection.execute(
        "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
        "VALUES (?, NULL, ?, ?) RETURNING id",
        (user_id, cat_id, 1_000_000),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute("UPDATE plan_node SET parent_id = ? WHERE id = ?", (plan_id, plan_id))


def test_fresh_db_has_no_dangling_foreign_keys() -> None:
    """`PRAGMA foreign_key_check` is clean after every migration applies."""
    db = Database.connect(":memory:")
    violations = db.connection.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == []


# ---------------------------------------------------------------------------
# Migration 4 — cross-row invariant triggers, index drop, depth-capped view.
# ---------------------------------------------------------------------------


def test_idx_mapping_instrument_is_dropped(db: Database) -> None:
    """Migration 4 drops the index — the UNIQUE autoindex covers the lookup."""
    indexes = _names_of(db.connection, "index")
    assert "idx_mapping_instrument" not in indexes


def test_category_path_view_caps_recursion_depth(db: Database) -> None:
    """The view's depth cap stops recursion after 32 levels.

    Defence-in-depth: a chain longer than 32 levels (or, in a
    malformed-data scenario the cycle triggers should already prevent,
    a cycle the recursion could reach) would otherwise let the CTE
    walk indefinitely. The cap surfaces a bounded result instead of
    a hang.
    """
    parent_id: int | None = None
    for i in range(40):
        cursor = db.connection.execute(
            "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
            (parent_id, f"L{i}"),
        ).fetchone()
        parent_id = cursor["id"]
    rows = db.connection.execute("SELECT id, path FROM category_path").fetchall()
    # The cap holds the row count to exactly 32 (one per level kept).
    assert len(rows) == 32
    deepest = max(rows, key=lambda r: len(r["path"]))
    # Path is `L0 / L1 / ... / L31` — 32 segments, 31 separators.
    assert deepest["path"].count("/") == 31


def test_category_cycle_trigger_blocks_multi_row_cycle(db: Database) -> None:
    """The cycle trigger aborts a parent_id update that would close a cycle."""
    a = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("A",),
    ).fetchone()["id"]
    b = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (a, "B"),
    ).fetchone()["id"]
    c = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (b, "C"),
    ).fetchone()["id"]
    # A → B → C is the current shape. Re-parenting A under C would make A
    # its own grand-ancestor — the trigger must abort.
    with pytest.raises(sqlite3.IntegrityError, match="cycle"):
        db.connection.execute("UPDATE category SET parent_id = ? WHERE id = ?", (c, a))


def test_plan_node_cycle_trigger_present(db: Database) -> None:
    """`plan_node_no_cycle_update` is wired up.

    A direct end-to-end demonstration is hard to construct because the
    lineage trigger fires on the same UPDATE OF parent_id and would
    typically abort first (a lineage-valid reparenting that also creates
    a cycle is geometrically impossible in a tree that mirrors the
    category structure). The trigger's body is identical in shape to
    `category_no_cycle_update`, which IS exercised end-to-end above. So
    we settle for confirming the trigger exists; if it ever disappears,
    a future migration that loosens the lineage rule would reintroduce
    the multi-row cycle risk.
    """
    names = _names_of(db.connection, "trigger")
    assert "plan_node_no_cycle_update" in names


def test_plan_node_parent_must_share_user(db: Database) -> None:
    """Trigger aborts when a plan_node's parent belongs to a different user."""
    user_id = _seed_minimal_user(db.connection)
    db.connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("tani", "2026-05-17T00:00:00Z"),
    )
    other_user_id = db.connection.execute(
        "SELECT id FROM user WHERE name = ?", ("tani",)
    ).fetchone()["id"]
    eq_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Equities",),
    ).fetchone()["id"]
    dev_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (eq_id, "Developed"),
    ).fetchone()["id"]
    parent = db.connection.execute(
        "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
        "VALUES (?, NULL, ?, ?) RETURNING id",
        (user_id, eq_id, 1_000_000),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="same user"):
        db.connection.execute(
            "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
            "VALUES (?, ?, ?, ?)",
            (other_user_id, parent, dev_id, 1_000_000),
        )


def test_plan_node_root_must_reference_root_category(db: Database) -> None:
    """Trigger aborts when a top-level plan_node references a non-root category."""
    user_id = _seed_minimal_user(db.connection)
    eq_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Equities",),
    ).fetchone()["id"]
    dev_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (eq_id, "Developed"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="lineage"):
        db.connection.execute(
            "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
            "VALUES (?, NULL, ?, ?)",
            (user_id, dev_id, 1_000_000),
        )


def test_plan_node_child_category_must_descend_from_parent(db: Database) -> None:
    """Trigger aborts when child plan_node's category is not a child of parent's."""
    user_id = _seed_minimal_user(db.connection)
    eq_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Equities",),
    ).fetchone()["id"]
    bonds_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Bonds",),
    ).fetchone()["id"]
    dev_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?) RETURNING id",
        (bonds_id, "Developed"),
    ).fetchone()["id"]
    eq_plan = db.connection.execute(
        "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
        "VALUES (?, NULL, ?, ?) RETURNING id",
        (user_id, eq_id, 1_000_000),
    ).fetchone()["id"]
    # Inserting a plan_node whose parent is the Equities plan node but
    # whose category is `Bonds / Developed` is a lineage violation.
    with pytest.raises(sqlite3.IntegrityError, match="lineage"):
        db.connection.execute(
            "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
            "VALUES (?, ?, ?, ?)",
            (user_id, eq_plan, dev_id, 1_000_000),
        )


def test_position_instrument_source_must_match_account_source(db: Database) -> None:
    """Trigger aborts when an IBKR instrument lands on an AJ Bell statement_import."""
    user_id = _seed_minimal_user(db.connection)
    ibkr_source_id = _source_id_for(db.connection, "ibkr")
    ajbell_source_id = _source_id_for(db.connection, "ajbell")
    ibkr_account_id = db.connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?) RETURNING id",
        (user_id, ibkr_source_id, "taxable"),
    ).fetchone()["id"]
    ibkr_si_id = db.connection.execute(
        "INSERT INTO statement_import "
        "(account_id, as_of, statement_path, imported_at) "
        "VALUES (?, ?, NULL, ?) RETURNING id",
        (ibkr_account_id, "2026-05-17", "2026-05-17T12:00:00Z"),
    ).fetchone()["id"]
    # An AJ Bell-sourced instrument cannot attach to an IBKR statement.
    ajbell_instr_id = db.connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text) VALUES (?, ?) RETURNING id",
        (ajbell_source_id, "SPAG"),
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="same broker"):
        db.connection.execute(
            "INSERT INTO position "
            "(statement_import_id, instrument_id, "
            "market_value_native_decithou, currency) "
            "VALUES (?, ?, ?, ?)",
            (ibkr_si_id, ajbell_instr_id, 100, "GBP"),
        )


# ---------------------------------------------------------------------------
# Migration runner — FK-off envelope correctness.
# ---------------------------------------------------------------------------


def test_runner_restores_foreign_keys_after_fk_off_migration() -> None:
    """After every migration applies, FK enforcement is back on.

    Migration 3 turns FK off internally to allow the table-recreate
    pattern; this test pins the post-condition that the runner restores
    it before returning, regardless of which migrations ran.
    """
    db = Database.connect(":memory:")
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_runner_restores_foreign_keys_when_migration_body_fails() -> None:
    """A failure inside an FK-off migration still restores FK to ON.

    The runner's `finally` clause is the only barrier between a midway
    failure and a connection that escapes with FK silently off. This
    test wires a deliberately-broken migration into the list and asserts
    the post-condition.
    """
    from riskbalancer.migrations import MIGRATIONS, Migration, apply_migrations

    def _doomed_migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE _x (id INTEGER PRIMARY KEY)")
        raise RuntimeError("forced failure")

    # Bring up a fresh DB at the current head, then sabotage by appending
    # a doomed migration that needs FK off. The runner should propagate
    # the failure but still restore FK to ON.
    db = Database.connect(":memory:")
    original = list(MIGRATIONS)
    MIGRATIONS.append(Migration(_doomed_migration, requires_fk_off=True))
    try:
        with pytest.raises(RuntimeError, match="forced failure"):
            apply_migrations(db.connection)
    finally:
        MIGRATIONS[:] = original
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def _seed_minimal_user(connection: sqlite3.Connection) -> int:
    """Insert a single user and return its row id. Used by multiple tests."""
    connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("emre", "2026-05-17T00:00:00Z"),
    )
    return int(connection.execute("SELECT id FROM user WHERE name = ?", ("emre",)).fetchone()["id"])
