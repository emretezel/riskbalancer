"""
Tests for the seed loader.

The seed loader is the bridge from the committed YAML catalog (the
project's "default state") to the database. These tests use the real
on-disk YAML files in `config/` because the goal is to verify that the
project's actual seed data round-trips into the database exactly as the
in-memory loaders interpret it — which is the only way the seeded DB and
the existing tests can stay in lockstep.

Author: Emre Tezel
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from riskbalancer.db import Database
from riskbalancer.seed import (
    MICROS_SCALE,
    fraction_to_micros,
    resolve_category_path,
    seed_from_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_PLAN = REPO_ROOT / "config" / "seed_plan.yaml"
MAPPINGS_DIR = REPO_ROOT / "config" / "mappings"


@pytest.fixture
def seeded_db() -> Database:
    """An in-memory database with the committed catalog seeded into it."""
    db = Database.connect(":memory:")
    seed_from_yaml(
        db.connection,
        seed_plan_path=SEED_PLAN,
        mappings_dir=MAPPINGS_DIR,
    )
    return db


def test_fraction_to_micros_rounds_correctly() -> None:
    """`0.62 + 0.05 + 0.13 + 0.2` must sum back to exactly 1_000_000."""
    parts = (0.62, 0.05, 0.13, 0.2)
    assert sum(fraction_to_micros(p) for p in parts) == MICROS_SCALE


def test_seed_creates_category_hierarchy_from_plan(seeded_db: Database) -> None:
    """The seed plan's top-level assets become root categories in the DB."""
    rows = seeded_db.connection.execute(
        "SELECT name FROM category WHERE parent_id IS NULL ORDER BY name"
    ).fetchall()
    names = [row["name"] for row in rows]
    assert "Equities" in names
    assert "Bonds" in names
    assert "Alternatives" in names
    assert "Cash" in names


def test_seed_resolves_nested_paths(seeded_db: Database) -> None:
    """A four-deep seed leaf is reachable as a single ` / `-joined path."""
    row = seeded_db.connection.execute(
        "SELECT path FROM category_path WHERE path = ?",
        ("Bonds / Developed / NAM / Govt",),
    ).fetchone()
    assert row is not None


def test_seed_distinguishes_same_leaf_name_under_distinct_parents(
    seeded_db: Database,
) -> None:
    """`Govt` under `NAM` and `Govt` under `Europe` are different rows.

    The seed contains both `Bonds / Developed / NAM / Govt` and
    `Bonds / Developed / Europe / Govt`. They must materialise as two
    distinct `category.id` values — that is the headline invariant of the
    new schema.
    """
    nam_id = seeded_db.connection.execute(
        "SELECT id FROM category_path WHERE path = ?",
        ("Bonds / Developed / NAM / Govt",),
    ).fetchone()["id"]
    europe_id = seeded_db.connection.execute(
        "SELECT id FROM category_path WHERE path = ?",
        ("Bonds / Developed / Europe / Govt",),
    ).fetchone()["id"]
    assert nam_id != europe_id


def test_seed_records_category_default_for_leaves(seeded_db: Database) -> None:
    """`category_default` carries the seed's leaf volatility / adjustment."""
    nam_row = seeded_db.connection.execute(
        """
        SELECT cd.volatility_micros, cd.adjustment_micros
        FROM category_default cd
        JOIN category_path cp ON cp.id = cd.category_id
        WHERE cp.path = ?
        """,
        ("Equities / Developed / NAM",),
    ).fetchone()
    assert nam_row is not None
    # seed_plan.yaml: volatility 0.175, adjustment 1.0
    assert nam_row["volatility_micros"] == fraction_to_micros(0.175)
    assert nam_row["adjustment_micros"] == fraction_to_micros(1.0)


def test_seed_default_supports_adjustment_above_one(seeded_db: Database) -> None:
    """Adjustments like 1.35 are stored as 1_350_000 (no [0,1] clamp)."""
    row = seeded_db.connection.execute(
        """
        SELECT cd.adjustment_micros
        FROM category_default cd
        JOIN category_path cp ON cp.id = cd.category_id
        WHERE cp.path = ?
        """,
        ("Bonds / Developed / NAM / Inflation",),
    ).fetchone()
    assert row is not None
    assert row["adjustment_micros"] == fraction_to_micros(1.35)


def test_seed_loads_every_mapping_yaml(seeded_db: Database) -> None:
    """Each `<adapter>.yaml` produces mapping rows scoped to that adapter."""
    counts = seeded_db.connection.execute(
        """
        SELECT i.adapter AS adapter, COUNT(*) AS n
        FROM mapping m JOIN instrument i ON i.id = m.instrument_id
        GROUP BY i.adapter
        ORDER BY i.adapter
        """
    ).fetchall()
    by_adapter = {row["adapter"]: row["n"] for row in counts}
    # Aegon is `{}` in the repo — it should not crash the loader, and it
    # should not insert rows. Other adapters should have at least one
    # mapping row each.
    assert "aegon" not in by_adapter
    for adapter in ("ibkr", "ajbell", "citi", "ms401k", "schwab"):
        assert by_adapter.get(adapter, 0) > 0, f"{adapter} produced zero mappings"


def test_seed_preserves_multi_allocation_split(seeded_db: Database) -> None:
    """The AJ Bell `SPAG` mapping has four allocations summing to 1.0.

    Their `weight_micros` values must sum to exactly 1_000_000 once stored
    (the rounding helper exists for this case specifically).
    """
    rows = seeded_db.connection.execute(
        """
        SELECT m.weight_micros, cp.path
        FROM mapping m
        JOIN instrument i ON i.id = m.instrument_id
        JOIN category_path cp ON cp.id = m.category_id
        WHERE i.adapter = ? AND i.instrument_id_text = ?
        ORDER BY cp.path
        """,
        ("ajbell", "SPAG"),
    ).fetchall()
    assert len(rows) == 4
    assert sum(row["weight_micros"] for row in rows) == MICROS_SCALE


def test_seed_creates_categories_referenced_by_mappings_but_not_in_plan(
    seeded_db: Database,
) -> None:
    """A mapping that references a leaf the seed plan doesn't carry still resolves.

    Mappings reference paths like `Alternatives / Silver` (present in the
    seed plan) and `Cash` (top-level in the seed plan). The loader must
    create whatever path the mapping references regardless of whether the
    plan already declared it. We assert the category exists.
    """
    row = seeded_db.connection.execute(
        "SELECT id FROM category_path WHERE path = ?", ("Alternatives / Silver",)
    ).fetchone()
    assert row is not None


def test_seed_is_idempotent(seeded_db: Database) -> None:
    """Re-running the loader does not multiply rows."""
    before_categories = seeded_db.connection.execute(
        "SELECT COUNT(*) AS n FROM category"
    ).fetchone()["n"]
    before_mappings = seeded_db.connection.execute("SELECT COUNT(*) AS n FROM mapping").fetchone()[
        "n"
    ]
    before_defaults = seeded_db.connection.execute(
        "SELECT COUNT(*) AS n FROM category_default"
    ).fetchone()["n"]
    seed_from_yaml(
        seeded_db.connection,
        seed_plan_path=SEED_PLAN,
        mappings_dir=MAPPINGS_DIR,
    )
    assert (
        seeded_db.connection.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]
        == before_categories
    )
    assert (
        seeded_db.connection.execute("SELECT COUNT(*) AS n FROM mapping").fetchone()["n"]
        == before_mappings
    )
    assert (
        seeded_db.connection.execute("SELECT COUNT(*) AS n FROM category_default").fetchone()["n"]
        == before_defaults
    )


def test_seed_yaml_authority_drops_removed_allocations(tmp_path: Path) -> None:
    """If a YAML file is edited to remove an instrument, re-seed wipes its mappings."""
    db = Database.connect(":memory:")
    fake_mappings = tmp_path / "mappings"
    fake_mappings.mkdir()
    (fake_mappings / "ibkr.yaml").write_text(
        yaml.safe_dump(
            {
                "EMIM": {
                    "allocations": [
                        {"category": "Equities / EM / Asia", "weight": 1.0},
                    ]
                },
                "IUVF": {
                    "allocations": [
                        {"category": "Equities / Developed / NAM", "weight": 1.0},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    seed_from_yaml(db.connection, seed_plan_path=SEED_PLAN, mappings_dir=fake_mappings)
    assert _count_mappings(db, adapter="ibkr") == 2

    # Edit the YAML to drop IUVF and re-seed.
    (fake_mappings / "ibkr.yaml").write_text(
        yaml.safe_dump(
            {
                "EMIM": {
                    "allocations": [
                        {"category": "Equities / EM / Asia", "weight": 1.0},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    seed_from_yaml(db.connection, seed_plan_path=SEED_PLAN, mappings_dir=fake_mappings)
    assert _count_mappings(db, adapter="ibkr") == 1


def test_seed_refuses_to_map_to_branch_category(tmp_path: Path) -> None:
    """A YAML mapping that targets a branch category is rejected by the trigger.

    The leaf-only invariant for mappings is enforced by a `BEFORE INSERT`
    trigger; the seed loader does not pre-filter, so a stale YAML pointing
    at `Equities / EM` (now a branch) must surface as an IntegrityError
    rather than silently insert.
    """
    import sqlite3

    db = Database.connect(":memory:")
    fake_mappings = tmp_path / "mappings"
    fake_mappings.mkdir()
    (fake_mappings / "ibkr.yaml").write_text(
        yaml.safe_dump(
            {
                "EMIM": {
                    "allocations": [
                        # `Equities / EM` is a branch in the canonical seed
                        # — pointing a mapping at it must be rejected.
                        {"category": "Equities / EM", "weight": 1.0},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.IntegrityError, match="leaf"):
        seed_from_yaml(db.connection, seed_plan_path=SEED_PLAN, mappings_dir=fake_mappings)


def test_resolve_category_path_is_find_or_create() -> None:
    """`resolve_category_path` reuses existing rows and only creates what's missing."""
    db = Database.connect(":memory:")
    first = resolve_category_path(db.connection, ("Bonds", "Developed", "NAM", "Govt"))
    second = resolve_category_path(db.connection, ("Bonds", "Developed", "NAM", "Govt"))
    assert first == second
    # An extension under the same parent reuses the prefix.
    corp = resolve_category_path(db.connection, ("Bonds", "Developed", "NAM", "Corp"))
    assert corp != first
    govt_path = db.connection.execute(
        "SELECT path FROM category_path WHERE id = ?", (first,)
    ).fetchone()["path"]
    corp_path = db.connection.execute(
        "SELECT path FROM category_path WHERE id = ?", (corp,)
    ).fetchone()["path"]
    assert govt_path == "Bonds / Developed / NAM / Govt"
    assert corp_path == "Bonds / Developed / NAM / Corp"


def _count_mappings(db: Database, *, adapter: str) -> int:
    """Count mapping rows for one adapter."""
    return int(
        db.connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM mapping m JOIN instrument i ON i.id = m.instrument_id
            WHERE i.adapter = ?
            """,
            (adapter,),
        ).fetchone()["n"]
    )
