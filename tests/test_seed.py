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

from riskbalancer.configuration import CategoryNode
from riskbalancer.db import Database
from riskbalancer.repositories import (
    find_or_create_user,
    load_plan_tree,
    upsert_category_attribute,
    write_plan_tree,
)
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


def test_seed_records_category_attribute_for_leaves(seeded_db: Database) -> None:
    """`category` carries the seed leaf's volatility and adjustment.

    Migration 6 merged the former `category_attribute` columns onto
    `category`. The row holds the leaf's intrinsic fundamentals — no
    parent-relative weight, no derivation. Plan weights live on
    `plan_node`.
    """
    nam_row = seeded_db.connection.execute(
        """
        SELECT c.volatility_micros, c.adjustment_micros
        FROM category c
        JOIN category_path cp ON cp.id = c.id
        WHERE cp.path = ?
        """,
        ("Equities / Developed / NAM",),
    ).fetchone()
    assert nam_row is not None
    # seed_plan.yaml: volatility 0.175, adjustment 1.0
    assert nam_row["volatility_micros"] == fraction_to_micros(0.175)
    assert nam_row["adjustment_micros"] == fraction_to_micros(1.0)


def test_seed_does_not_record_branches_in_category_attribute(seeded_db: Database) -> None:
    """Seed branches have NULL vol/adj on `category`.

    Branch-level volatility/adjustment is not a fact the schema records;
    a user who wants to hold a branch (e.g. `Equities / Developed`) as
    a plan-leaf must supply explicit vol/adj at plan-creation time. The
    paired-NULL CHECK guarantees the two columns are unset together.
    """
    row = seeded_db.connection.execute(
        """
        SELECT c.volatility_micros, c.adjustment_micros
        FROM category c
        JOIN category_path cp ON cp.id = c.id
        WHERE cp.path = ?
        """,
        ("Equities / Developed",),
    ).fetchone()
    assert row is not None
    assert row["volatility_micros"] is None
    assert row["adjustment_micros"] is None


def test_seed_default_supports_adjustment_above_one(seeded_db: Database) -> None:
    """Adjustments like 1.35 are stored as 1_350_000 (no [0,1] clamp)."""
    row = seeded_db.connection.execute(
        """
        SELECT c.adjustment_micros
        FROM category c
        JOIN category_path cp ON cp.id = c.id
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
        SELECT s.adapter AS adapter, COUNT(*) AS n
        FROM mapping m
        JOIN instrument i ON i.id = m.instrument_id
        JOIN source s ON s.id = i.source_id
        GROUP BY s.adapter
        ORDER BY s.adapter
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
        JOIN source s ON s.id = i.source_id
        JOIN category_path cp ON cp.id = m.category_id
        WHERE s.adapter = ? AND i.instrument_id_text = ?
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
    before_attributes = seeded_db.connection.execute(
        "SELECT COUNT(*) AS n FROM category WHERE volatility_micros IS NOT NULL"
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
        seeded_db.connection.execute(
            "SELECT COUNT(*) AS n FROM category WHERE volatility_micros IS NOT NULL"
        ).fetchone()["n"]
        == before_attributes
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


def test_load_plan_tree_raises_when_leaf_has_no_category_attribute(tmp_path: Path) -> None:
    """A plan-leaf whose category has NULL vol/adj cannot load.

    Migration 6 merged vol/adj onto `category` (nullable, paired). A
    plan-leaf referencing a category with NULL vol/adj is a
    data-integrity error — the loader refuses to invent fallback values
    and raises a typed error pinpointing the offending leaf.
    """
    db = Database.connect(":memory:")
    user_id = find_or_create_user(db.connection, "emre")
    # Build the category row but deliberately leave vol/adj NULL so the
    # schema's "no derived vol/adj" rule is exercised end-to-end.
    cash_id = db.connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (NULL, ?) RETURNING id",
        ("Cash",),
    ).fetchone()["id"]
    db.connection.execute(
        "INSERT INTO plan_node (user_id, parent_id, category_id, weight_micros) "
        "VALUES (?, NULL, ?, ?)",
        (user_id, cash_id, MICROS_SCALE),
    )
    with pytest.raises(ValueError, match="no volatility/adjustment recorded"):
        load_plan_tree(db.connection, user_id)


def test_plan_tree_round_trip_preserves_explicit_leaf_attributes(tmp_path: Path) -> None:
    """Writing a plan-leaf and reading it back returns the same vol/adj.

    Exercises the canonical path: the walker collects explicit vol/adj
    for a plan-leaf, `write_plan_tree` updates the merged columns on
    `category`, and `load_plan_tree` reads them back without
    transformation. No derivation, no fallback — what goes in comes out.
    """
    db = Database.connect(":memory:")
    user_id = find_or_create_user(db.connection, "emre")
    plan = [
        CategoryNode(
            name="Equities",
            weight=0.6,
            volatility=0.18,
            adjustment=0.95,
            children=[],
        ),
        CategoryNode(
            name="Bonds",
            weight=0.4,
            volatility=0.05,
            adjustment=1.10,
            children=[],
        ),
    ]
    write_plan_tree(db.connection, user_id, plan)
    loaded = load_plan_tree(db.connection, user_id)
    by_name = {node.name: node for node in loaded}
    assert by_name["Equities"].volatility == pytest.approx(0.18)
    assert by_name["Equities"].adjustment == pytest.approx(0.95)
    assert by_name["Bonds"].volatility == pytest.approx(0.05)
    assert by_name["Bonds"].adjustment == pytest.approx(1.10)


def test_branch_as_leaf_requires_explicit_vol_adj(seeded_db: Database) -> None:
    """A user holding a seed-branch as a plan-leaf must set vol/adj explicitly.

    The seed plan declares `Equities / EM` as a branch (Asia / EMEA /
    Americas children), so the merged `category` row has NULL vol/adj.
    A user whose plan stops at EM-as-a-leaf without supplying explicit
    vol/adj on the in-memory node and without recorded fundamentals on
    the category cannot be persisted: the writer raises rather than
    fabricating values.
    """
    user_id = find_or_create_user(seeded_db.connection, "emre")
    em_id = seeded_db.connection.execute(
        "SELECT id FROM category_path WHERE path = ?",
        ("Equities / EM",),
    ).fetchone()["id"]
    # Sanity check: the seed-loaded branch has NULL vol/adj.
    assert (
        seeded_db.connection.execute(
            "SELECT volatility_micros FROM category WHERE id = ?",
            (em_id,),
        ).fetchone()["volatility_micros"]
        is None
    )
    plan = [
        CategoryNode(
            name="Equities",
            weight=1.0,
            children=[
                CategoryNode(name="EM", weight=1.0, volatility=None),
            ],
        ),
    ]
    with pytest.raises(ValueError, match="has no in-memory volatility"):
        write_plan_tree(seeded_db.connection, user_id, plan)
    # Once the walker has collected explicit fundamentals, the same
    # plan persists successfully because the category now has vol/adj.
    upsert_category_attribute(
        seeded_db.connection,
        category_id=em_id,
        volatility=0.22,
        adjustment=0.9,
    )
    write_plan_tree(seeded_db.connection, user_id, plan)
    loaded = load_plan_tree(seeded_db.connection, user_id)
    em_node = loaded[0].children[0]
    assert em_node.volatility == pytest.approx(0.22)
    assert em_node.adjustment == pytest.approx(0.9)


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
            FROM mapping m
            JOIN instrument i ON i.id = m.instrument_id
            JOIN source s ON s.id = i.source_id
            WHERE s.adapter = ?
            """,
            (adapter,),
        ).fetchone()["n"]
    )
