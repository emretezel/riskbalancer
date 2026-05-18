"""
Tests for `rb category list/add/update/delete` and the underlying
repository accessors.

Categories form the global tree; vol/adj live on the row itself per the
schema's §3.3 (merged in migration 6). The fundamentals are paired by a
CHECK — both set or both NULL.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import populate_test_catalog, sandboxed_paths, write_plan_yaml_to_db
from riskbalancer.cli import (
    cmd_category_add,
    cmd_category_delete,
    cmd_category_list,
    cmd_category_update,
    cmd_instrument_add,
    cmd_mapping_add,
)
from riskbalancer.db import Database
from riskbalancer.repositories import (
    create_category,
    delete_category,
    find_category_by_path,
    find_or_create_user,
    get_category_by_id,
    get_category_path,
    list_categories_tree,
    plan_leaf_category_ids_for_user,
    update_category,
)


@pytest.fixture()
def paths(tmp_path: Path):
    """Sandboxed paths + the seeded test catalog."""
    p = sandboxed_paths(tmp_path)
    populate_test_catalog(p)
    return p


@pytest.fixture()
def empty_paths(tmp_path: Path):
    """Sandboxed paths with no seeded categories — for empty-tree tests."""
    return sandboxed_paths(tmp_path)


def _add_args(
    *,
    name: str,
    parent: str | None = None,
    volatility: float | None = None,
    adjustment: float | None = None,
):
    return argparse.Namespace(
        parent=parent,
        name=name,
        volatility=volatility,
        adjustment=adjustment,
    )


def _update_args(
    *,
    category_id: int,
    name: str | None = None,
    volatility: float | None = None,
    adjustment: float | None = None,
    clear_fundamentals: bool = False,
):
    return argparse.Namespace(
        category_id=category_id,
        name=name,
        volatility=volatility,
        adjustment=adjustment,
        clear_fundamentals=clear_fundamentals,
    )


def _delete_args(*, category_id: int):
    return argparse.Namespace(category_id=category_id)


def _list_args(*, user: str | None = None):
    return argparse.Namespace(user=user)


# ---------------------------------------------------------------------------
# Repository: list_categories_tree
# ---------------------------------------------------------------------------


def test_list_categories_tree_returns_depth_first_preorder(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        rows = list_categories_tree(db.connection)
        paths_seen = [row["path"] for row in rows]
        # "Equities" must appear before "Equities / Developed" must appear before
        # "Equities / Developed / NAM". This is depth-first pre-order — the
        # property the renderer relies on for its indent levels.
        equities_idx = paths_seen.index("Equities")
        developed_idx = paths_seen.index("Equities / Developed")
        nam_idx = paths_seen.index("Equities / Developed / NAM")
        assert equities_idx < developed_idx < nam_idx
    finally:
        db.close()


def test_list_categories_tree_carries_depth_and_fundamentals(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        by_path = {row["path"]: row for row in list_categories_tree(db.connection)}
        # Top-level branch: depth 0, NULL fundamentals.
        assert by_path["Equities"]["depth"] == 0
        assert by_path["Equities"]["volatility"] is None
        assert by_path["Equities"]["adjustment"] is None
        # Leaf at depth 2: vol/adj both set.
        nam = by_path["Equities / Developed / NAM"]
        assert nam["depth"] == 2
        assert nam["volatility"] == pytest.approx(0.17)
        assert nam["adjustment"] == pytest.approx(1.0)
    finally:
        db.close()


def test_list_categories_tree_empty_returns_empty_list(empty_paths) -> None:
    db = Database.connect(empty_paths.db_path)
    try:
        assert list_categories_tree(db.connection) == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Repository: create_category / update_category / delete_category
# ---------------------------------------------------------------------------


def test_create_category_rejects_unpaired_fundamentals(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        with pytest.raises(ValueError, match="both or neither"):
            create_category(
                db.connection, parent_id=None, name="Alt", volatility=0.2, adjustment=None
            )
        with pytest.raises(ValueError, match="both or neither"):
            create_category(
                db.connection, parent_id=None, name="Alt", volatility=None, adjustment=1.0
            )
    finally:
        db.close()


def test_create_category_rejects_negative_fundamentals(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        with pytest.raises(ValueError, match="volatility"):
            create_category(
                db.connection, parent_id=None, name="Alt", volatility=-0.1, adjustment=1.0
            )
        with pytest.raises(ValueError, match="adjustment"):
            create_category(
                db.connection, parent_id=None, name="Alt", volatility=0.2, adjustment=-0.5
            )
    finally:
        db.close()


def test_update_category_rejects_unpaired_fundamentals(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
        with pytest.raises(ValueError, match="both or neither"):
            update_category(db.connection, category_id=category_id, volatility=0.2)
        with pytest.raises(ValueError, match="cannot be combined"):
            update_category(
                db.connection,
                category_id=category_id,
                volatility=0.2,
                adjustment=1.0,
                set_fundamentals_null=True,
            )
    finally:
        db.close()


def test_update_category_requires_at_least_one_change(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
        with pytest.raises(ValueError, match="at least one field"):
            update_category(db.connection, category_id=category_id)
    finally:
        db.close()


def test_update_category_clears_fundamentals(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
        update_category(db.connection, category_id=category_id, set_fundamentals_null=True)
        db.connection.commit()
        row = get_category_by_id(db.connection, category_id)
        assert row is not None
        assert row["volatility"] is None
        assert row["adjustment"] is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_category_add
# ---------------------------------------------------------------------------


def test_category_add_top_level_creates_row(empty_paths, capsys) -> None:
    rc = cmd_category_add(_add_args(name="Equities"), paths=empty_paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Added category" in out
    assert "Equities" in out

    db = Database.connect(empty_paths.db_path)
    try:
        rows = list_categories_tree(db.connection)
        assert [row["path"] for row in rows] == ["Equities"]
        assert rows[0]["volatility"] is None
        assert rows[0]["adjustment"] is None
    finally:
        db.close()


def test_category_add_with_parent_resolves_path(paths, capsys) -> None:
    rc = cmd_category_add(
        _add_args(
            parent="Equities / Developed",
            name="Pacific",
            volatility=0.19,
            adjustment=1.0,
        ),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        new_id = find_category_by_path(db.connection, "Equities / Developed / Pacific")
        assert new_id is not None
        row = get_category_by_id(db.connection, new_id)
        assert row is not None
        assert row["volatility"] == pytest.approx(0.19)
        assert row["adjustment"] == pytest.approx(1.0)
    finally:
        db.close()


def test_category_add_unknown_parent_errors(paths, capsys) -> None:
    rc = cmd_category_add(
        _add_args(parent="Made / Up", name="Whatever"),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "parent path 'Made / Up' not found" in err


def test_category_add_unpaired_fundamentals_errors(paths, capsys) -> None:
    rc = cmd_category_add(
        _add_args(parent="Equities", name="Alt", volatility=0.2),
        paths=paths,
    )
    assert rc == 1
    assert "both or neither" in capsys.readouterr().err


def test_category_add_duplicate_sibling_name_errors(paths, capsys) -> None:
    """`UNIQUE (parent_id, name)` blocks two siblings sharing a name."""
    rc = cmd_category_add(
        _add_args(parent="Equities", name="Developed"),
        paths=paths,
    )
    assert rc == 1
    # The IntegrityError surfaces as a friendly error line; the exact text is
    # whatever SQLite reports, but the prefix should be ours.
    assert "category add failed" in capsys.readouterr().err


def test_category_add_top_level_duplicate_blocked(empty_paths, capsys) -> None:
    """The partial unique index on top-level names blocks duplicates."""
    cmd_category_add(_add_args(name="Equities"), paths=empty_paths)
    capsys.readouterr()

    rc = cmd_category_add(_add_args(name="Equities"), paths=empty_paths)
    assert rc == 1
    assert "category add failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_category_update
# ---------------------------------------------------------------------------


def test_category_update_renames(paths, capsys) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_update(
        _update_args(category_id=category_id, name="Asia Pacific"),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        assert find_category_by_path(db.connection, "Equities / EM / Asia") is None
        assert find_category_by_path(db.connection, "Equities / EM / Asia Pacific") is not None
    finally:
        db.close()


def test_category_update_changes_fundamentals(paths, capsys) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_update(
        _update_args(category_id=category_id, volatility=0.2, adjustment=1.1),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        row = get_category_by_id(db.connection, category_id)
        assert row is not None
        assert row["volatility"] == pytest.approx(0.2)
        assert row["adjustment"] == pytest.approx(1.1)
    finally:
        db.close()


def test_category_update_clear_fundamentals(paths, capsys) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_update(
        _update_args(category_id=category_id, clear_fundamentals=True),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        row = get_category_by_id(db.connection, category_id)
        assert row is not None
        assert row["volatility"] is None
        assert row["adjustment"] is None
    finally:
        db.close()


def test_category_update_requires_a_field(paths, capsys) -> None:
    rc = cmd_category_update(_update_args(category_id=1), paths=paths)
    assert rc == 1
    assert "at least one of" in capsys.readouterr().err


def test_category_update_rejects_clear_with_explicit_values(paths, capsys) -> None:
    rc = cmd_category_update(
        _update_args(
            category_id=1,
            volatility=0.2,
            adjustment=1.0,
            clear_fundamentals=True,
        ),
        paths=paths,
    )
    assert rc == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_category_update_unpaired_fundamentals_errors(paths, capsys) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
    finally:
        db.close()
    rc = cmd_category_update(
        _update_args(category_id=category_id, volatility=0.25),
        paths=paths,
    )
    assert rc == 1
    assert "must be passed together" in capsys.readouterr().err


def test_category_update_unknown_id_errors(paths, capsys) -> None:
    rc = cmd_category_update(_update_args(category_id=9999, name="x"), paths=paths)
    assert rc == 1
    assert "no category with id 9999" in capsys.readouterr().err


def test_category_update_rename_conflict_with_sibling(paths, capsys) -> None:
    """Renaming to an existing sibling name must fail (UNIQUE (parent_id, name))."""
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        assert category_id is not None
    finally:
        db.close()
    # Add a sibling first so there is a name to collide with.
    cmd_category_add(
        _add_args(parent="Equities / EM", name="Frontier", volatility=0.25, adjustment=1.0),
        paths=paths,
    )
    capsys.readouterr()

    rc = cmd_category_update(_update_args(category_id=category_id, name="Frontier"), paths=paths)
    assert rc == 1
    assert "category update failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_category_delete
# ---------------------------------------------------------------------------


def test_category_delete_removes_unreferenced_leaf(paths, capsys) -> None:
    """A leaf with no plan_node / mapping references can be deleted cleanly."""
    cmd_category_add(
        _add_args(parent="Equities / EM", name="Frontier", volatility=0.25, adjustment=1.0),
        paths=paths,
    )
    capsys.readouterr()

    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / EM / Frontier")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_delete(_delete_args(category_id=category_id), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        assert find_category_by_path(db.connection, "Equities / EM / Frontier") is None
    finally:
        db.close()


def test_category_delete_unknown_id_errors(paths, capsys) -> None:
    rc = cmd_category_delete(_delete_args(category_id=9999), paths=paths)
    assert rc == 1
    assert "no category with id 9999" in capsys.readouterr().err


def test_category_delete_blocked_by_child_category(paths, capsys) -> None:
    """`category.parent_id REFERENCES category(id) ON DELETE RESTRICT`."""
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed")
        assert category_id is not None
    finally:
        db.close()
    rc = cmd_category_delete(_delete_args(category_id=category_id), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "still referenced" in err
    db = Database.connect(paths.db_path)
    try:
        assert find_category_by_path(db.connection, "Equities / Developed") is not None
    finally:
        db.close()


def test_category_delete_blocked_by_mapping(paths, capsys) -> None:
    """`mapping.category_id REFERENCES category(id) ON DELETE RESTRICT`."""
    # Need an instrument + mapping on the target category.
    cmd_instrument_add(
        argparse.Namespace(source="ibkr", id="EMIM", description=None),
        paths=paths,
    )
    cmd_mapping_add(
        argparse.Namespace(
            source="ibkr",
            instrument="EMIM",
            category="Equities / EM / Asia",
            weight=None,
        ),
        paths=paths,
    )
    capsys.readouterr()

    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_delete(_delete_args(category_id=category_id), paths=paths)
    assert rc == 1
    assert "still referenced" in capsys.readouterr().err


def test_category_delete_blocked_by_plan_node(paths, capsys) -> None:
    """`plan_node.category_id REFERENCES category(id) ON DELETE RESTRICT`."""
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, paths.user)
        db.connection.commit()
    finally:
        db.close()
    # Write a small plan that adopts `Equities / EM / Asia` as a leaf.
    write_plan_yaml_to_db(
        paths,
        """
assets:
  - name: Equities
    weight: 1.0
    children:
      - name: EM
        weight: 1.0
        children:
          - name: Asia
            weight: 1.0
            volatility: 0.22
            adjustment: 1.0
""",
    )

    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        assert category_id is not None
    finally:
        db.close()

    rc = cmd_category_delete(_delete_args(category_id=category_id), paths=paths)
    assert rc == 1
    assert "still referenced" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_category_list
# ---------------------------------------------------------------------------


def test_category_list_empty_prints_placeholder(empty_paths, capsys) -> None:
    rc = cmd_category_list(_list_args(), paths=empty_paths)
    assert rc == 0
    assert "(no categories)" in capsys.readouterr().out


def test_category_list_renders_indented_tree(paths, capsys) -> None:
    rc = cmd_category_list(_list_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ID" in out and "PATH" in out and "VOL" in out and "ADJ" in out
    # Indented leaf name appears (depth 2 → 4 spaces).
    assert "    NAM" in out
    # Branch row uses em-dashes for vol/adj.
    assert "—" in out


def test_category_list_user_flag_marks_plan_leaves(paths, capsys) -> None:
    """`--user` adds a `(plan-leaf)` annotation on rows the user's plan adopts."""
    # Build a plan that adopts `Equities / EM / Asia` and `Equities / Developed / NAM` as leaves.
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, paths.user)
        db.connection.commit()
    finally:
        db.close()
    write_plan_yaml_to_db(
        paths,
        """
assets:
  - name: Equities
    weight: 1.0
    children:
      - name: Developed
        weight: 0.7
        children:
          - name: NAM
            weight: 1.0
            volatility: 0.17
            adjustment: 1.0
      - name: EM
        weight: 0.3
        children:
          - name: Asia
            weight: 1.0
            volatility: 0.22
            adjustment: 1.0
""",
    )

    rc = cmd_category_list(_list_args(user=paths.user), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out

    # Look up the actual category ids so we pick the right rendered rows —
    # both `Equities / Developed / NAM` and `Bonds / Developed / NAM` exist
    # in the seeded test catalog and render with the same leaf name.
    db = Database.connect(paths.db_path)
    try:
        equities_nam_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        bonds_nam_id = find_category_by_path(db.connection, "Bonds / Developed / NAM")
        asia_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        equities_developed_id = find_category_by_path(db.connection, "Equities / Developed")
    finally:
        db.close()

    def line_for_id(category_id: int | None) -> str:
        assert category_id is not None
        return next(
            line for line in out.splitlines() if line.lstrip().startswith(f"{category_id} ")
        )

    assert "(plan-leaf)" in line_for_id(equities_nam_id)
    assert "(plan-leaf)" in line_for_id(asia_id)
    # The Bonds-side NAM is NOT adopted by this user's plan.
    assert "(plan-leaf)" not in line_for_id(bonds_nam_id)
    # Branch rows like `Developed` must NOT carry the marker.
    assert "(plan-leaf)" not in line_for_id(equities_developed_id)


def test_category_list_user_without_plan_warns_but_succeeds(paths, capsys) -> None:
    """An unknown / planless user just runs without the marker."""
    rc = cmd_category_list(_list_args(user="ghost"), paths=paths)
    assert rc == 0
    err = capsys.readouterr().err
    assert "user 'ghost' has no rows" in err


# ---------------------------------------------------------------------------
# Repository: plan_leaf_category_ids_for_user
# ---------------------------------------------------------------------------


def test_plan_leaf_category_ids_for_user_returns_only_leaves(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, paths.user)
        db.connection.commit()
    finally:
        db.close()
    write_plan_yaml_to_db(
        paths,
        """
assets:
  - name: Equities
    weight: 1.0
    children:
      - name: EM
        weight: 1.0
        children:
          - name: Asia
            weight: 1.0
            volatility: 0.22
            adjustment: 1.0
""",
    )

    db = Database.connect(paths.db_path)
    try:
        from riskbalancer.repositories import find_user_id

        user_id = find_user_id(db.connection, paths.user)
        assert user_id is not None
        leaf_ids = plan_leaf_category_ids_for_user(db.connection, user_id)
        # Should contain only the Asia category, not Equities or EM (those are branches).
        asia_id = find_category_by_path(db.connection, "Equities / EM / Asia")
        em_id = find_category_by_path(db.connection, "Equities / EM")
        equities_id = find_category_by_path(db.connection, "Equities")
        assert asia_id in leaf_ids
        assert em_id not in leaf_ids
        assert equities_id not in leaf_ids
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Repository: get_category_by_id / delete_category direct calls
# ---------------------------------------------------------------------------


def test_get_category_by_id_returns_none_on_missing(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        assert get_category_by_id(db.connection, 9999) is None
    finally:
        db.close()


def test_delete_category_returns_false_on_missing(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        assert delete_category(db.connection, 9999) is False
    finally:
        db.close()


def test_get_category_path_round_trips_via_view(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        category_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        assert category_id is not None
        assert get_category_path(db.connection, category_id) == "Equities / Developed / NAM"
    finally:
        db.close()
