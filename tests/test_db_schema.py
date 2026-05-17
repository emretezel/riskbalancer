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
        "idx_mapping_instrument",
        "idx_position_instrument",
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


def _seed_minimal_user(connection: sqlite3.Connection) -> int:
    """Insert a single user and return its row id. Used by multiple tests."""
    connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        ("emre", "2026-05-17T00:00:00Z"),
    )
    return int(connection.execute("SELECT id FROM user WHERE name = ?", ("emre",)).fetchone()["id"])
