"""
Tests for `rb mapping list/add/update/delete` and the underlying repository
accessors.

These commands maintain the global `mapping` table — there is no per-user
scope. The schema's leaf-only trigger and weight-range CHECK do the heavy
lifting on row validity; the CLI adds the cross-row "weights sum to 1.0"
warning that's an application-level invariant.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import populate_test_catalog, sandboxed_paths
from riskbalancer.cli import (
    cmd_mapping_add,
    cmd_mapping_delete,
    cmd_mapping_list,
    cmd_mapping_update,
)
from riskbalancer.db import Database
from riskbalancer.repositories import (
    MICROS_SCALE,
    find_category_by_path,
    find_instrument_by_natural_key,
    find_mapping_by_id,
    find_or_create_instrument,
    get_mappings_for_instrument,
    get_source_id,
    list_mappings,
    weight_sum_micros_for_instrument,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def paths(tmp_path: Path):
    """Sandboxed paths + a minimal seeded catalog so categories exist."""
    p = sandboxed_paths(tmp_path)
    populate_test_catalog(p)
    return p


def _seed_instrument(paths, adapter: str, instrument_id_text: str) -> int:
    """Create an instrument row directly so the tests don't depend on import."""
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, adapter)
        instrument_id = find_or_create_instrument(
            db.connection,
            source_id=source_id,
            instrument_id_text=instrument_id_text,
            description=f"{instrument_id_text} description",
        )
        db.connection.commit()
        return instrument_id
    finally:
        db.close()


def _add_args(*, source: str, instrument: str, category: str, weight: str | None = None):
    return argparse.Namespace(
        source=source, instrument=instrument, category=category, weight=weight
    )


def _list_args(
    *,
    source: str | None = None,
    instrument: str | None = None,
    category: str | None = None,
):
    return argparse.Namespace(source=source, instrument=instrument, category=category)


def _update_args(*, mapping_id: int, category: str | None = None, weight: str | None = None):
    return argparse.Namespace(mapping_id=mapping_id, category=category, weight=weight)


def _delete_args(*, mapping_id: int):
    return argparse.Namespace(mapping_id=mapping_id)


# ---------------------------------------------------------------------------
# Repository: list_mappings filters
# ---------------------------------------------------------------------------


def test_list_mappings_returns_rows_across_filters(paths) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")
    _seed_instrument(paths, "ajbell", "SPAG")
    # Two ibkr rows + one ajbell row.
    assert (
        cmd_mapping_add(
            _add_args(source="ibkr", instrument="EMIM", category="Equities / EM / Asia"),
            paths=paths,
        )
        == 0
    )
    assert (
        cmd_mapping_add(
            _add_args(
                source="ajbell",
                instrument="SPAG",
                category="Equities / Developed / NAM",
                weight="62%",
            ),
            paths=paths,
        )
        == 0
    )

    db = Database.connect(paths.db_path)
    try:
        all_rows = list_mappings(db.connection)
        assert {(r["adapter"], r["instrument_id_text"]) for r in all_rows} == {
            ("ibkr", "EMIM"),
            ("ajbell", "SPAG"),
        }
        ibkr_only = list_mappings(db.connection, adapter="ibkr")
        assert {r["instrument_id_text"] for r in ibkr_only} == {"EMIM"}
        emim_only = list_mappings(db.connection, instrument_id_text="EMIM")
        assert {r["adapter"] for r in emim_only} == {"ibkr"}
        em_only = list_mappings(db.connection, category_path="Equities / EM / Asia")
        assert {r["instrument_id_text"] for r in em_only} == {"EMIM"}
    finally:
        db.close()


def test_find_category_by_path_round_trips_canonical_separators(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        # The test catalog defines "Equities / Developed / NAM" as a leaf.
        canonical_id = find_category_by_path(db.connection, "Equities / Developed / NAM")
        squished_id = find_category_by_path(db.connection, "Equities/Developed/NAM")
        spaced_id = find_category_by_path(db.connection, "  Equities  /  Developed  /  NAM  ")
        assert canonical_id is not None
        assert canonical_id == squished_id == spaced_id
    finally:
        db.close()


def test_find_category_by_path_returns_none_for_unknown(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        assert find_category_by_path(db.connection, "Made / Up / Path") is None
        assert find_category_by_path(db.connection, "") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_mapping_add
# ---------------------------------------------------------------------------


def test_mapping_add_inserts_row_and_no_warning_at_100pct(paths, capsys) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")

    rc = cmd_mapping_add(
        _add_args(source="ibkr", instrument="EMIM", category="Equities / EM / Asia"),
        paths=paths,
    )
    assert rc == 0

    captured = capsys.readouterr()
    assert "Added mapping" in captured.out
    # 100% allocation → no warning.
    assert captured.err == ""

    db = Database.connect(paths.db_path)
    try:
        rows = list_mappings(db.connection, instrument_id_text="EMIM")
        assert len(rows) == 1
        assert rows[0]["weight_micros"] == MICROS_SCALE
        assert rows[0]["category_path"] == "Equities / EM / Asia"
    finally:
        db.close()


def test_mapping_add_supports_percent_weight(paths, capsys) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")

    rc = cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="62%",
        ),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        rows = list_mappings(db.connection, instrument_id_text="SPAG")
        assert rows[0]["weight_micros"] == 620_000
    finally:
        db.close()


def test_mapping_add_supports_fractional_weight(paths) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")

    rc = cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        rows = list_mappings(db.connection, instrument_id_text="SPAG")
        assert rows[0]["weight_micros"] == 620_000
    finally:
        db.close()


def test_mapping_add_warns_when_total_below_100pct(paths, capsys) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")

    # Add a single 62% mapping; the remaining 38% is missing.
    rc = cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "62.00%" in err
    assert "expected 100%" in err


def test_mapping_add_clears_warning_after_siblings_sum_to_100pct(paths, capsys) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")

    # First row at 62% — warning.
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    capsys.readouterr()
    # Second row at 38% completes the allocation — no warning this time.
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / Europe",
            weight="0.38",
        ),
        paths=paths,
    )
    captured = capsys.readouterr()
    assert captured.err == ""


def test_mapping_add_rejects_branch_category(paths, capsys) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")

    # `Equities` is a branch (has Developed and EM children); mapping to it
    # must be rejected with a clean error message, not a raw trigger trace.
    rc = cmd_mapping_add(
        _add_args(source="ibkr", instrument="EMIM", category="Equities"),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a leaf" in err

    db = Database.connect(paths.db_path)
    try:
        assert list_mappings(db.connection) == []
    finally:
        db.close()


def test_mapping_add_rejects_unknown_instrument(paths, capsys) -> None:
    rc = cmd_mapping_add(
        _add_args(source="ibkr", instrument="NEVER_HEARD_OF", category="Equities / EM / Asia"),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "instrument 'NEVER_HEARD_OF' not found" in err


def test_mapping_add_rejects_unknown_category_path(paths, capsys) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")
    rc = cmd_mapping_add(
        _add_args(source="ibkr", instrument="EMIM", category="Equities / Made / Up"),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "category path 'Equities / Made / Up' not found" in err


def test_mapping_add_rejects_zero_weight(paths, capsys) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")
    rc = cmd_mapping_add(
        _add_args(
            source="ibkr",
            instrument="EMIM",
            category="Equities / EM / Asia",
            weight="0",
        ),
        paths=paths,
    )
    assert rc == 1
    assert "positive" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_mapping_update
# ---------------------------------------------------------------------------


def _add_and_get_id(
    paths, *, source: str, instrument: str, category: str, weight: str | None = None
) -> int:
    _seed_instrument(paths, source, instrument)
    cmd_mapping_add(
        _add_args(source=source, instrument=instrument, category=category, weight=weight),
        paths=paths,
    )
    db = Database.connect(paths.db_path)
    try:
        rows = list_mappings(db.connection, instrument_id_text=instrument)
        return rows[0]["id"]
    finally:
        db.close()


def test_mapping_update_changes_weight(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_update(_update_args(mapping_id=mapping_id, weight="0.5"), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        row = find_mapping_by_id(db.connection, mapping_id)
        assert row is not None
        assert row["weight_micros"] == 500_000
    finally:
        db.close()
    # 50% total → warning emitted.
    assert "50.00%" in capsys.readouterr().err


def test_mapping_update_changes_category(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_update(
        _update_args(mapping_id=mapping_id, category="Equities / Developed / NAM"),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        rows = list_mappings(db.connection, instrument_id_text="EMIM")
        assert rows[0]["category_path"] == "Equities / Developed / NAM"
    finally:
        db.close()


def test_mapping_update_rejects_branch_target(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_update(_update_args(mapping_id=mapping_id, category="Equities"), paths=paths)
    assert rc == 1
    assert "not a leaf" in capsys.readouterr().err


def test_mapping_update_requires_at_least_one_field(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_update(_update_args(mapping_id=mapping_id), paths=paths)
    assert rc == 1
    assert "at least one of --category or --weight" in capsys.readouterr().err


def test_mapping_update_errors_on_unknown_id(paths, capsys) -> None:
    rc = cmd_mapping_update(_update_args(mapping_id=999, weight="0.5"), paths=paths)
    assert rc == 1
    assert "no mapping with id 999" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_mapping_delete
# ---------------------------------------------------------------------------


def test_mapping_delete_removes_row(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_delete(_delete_args(mapping_id=mapping_id), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        assert list_mappings(db.connection) == []
    finally:
        db.close()


def test_mapping_delete_warns_when_instrument_now_unmapped(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    cmd_mapping_delete(_delete_args(mapping_id=mapping_id), paths=paths)
    err = capsys.readouterr().err
    assert "no longer has any mappings" in err


def test_mapping_delete_warns_when_remaining_weights_short_of_100pct(paths, capsys) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / Europe",
            weight="0.38",
        ),
        paths=paths,
    )
    capsys.readouterr()

    db = Database.connect(paths.db_path)
    try:
        nam_row = next(
            r
            for r in list_mappings(db.connection, instrument_id_text="SPAG")
            if r["category_path"] == "Equities / Developed / NAM"
        )
    finally:
        db.close()

    rc = cmd_mapping_delete(_delete_args(mapping_id=nam_row["id"]), paths=paths)
    assert rc == 0
    err = capsys.readouterr().err
    assert "38.00%" in err


def test_mapping_delete_errors_on_unknown_id(paths, capsys) -> None:
    rc = cmd_mapping_delete(_delete_args(mapping_id=999), paths=paths)
    assert rc == 1
    assert "no mapping with id 999" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_mapping_list
# ---------------------------------------------------------------------------


def test_mapping_list_prints_no_rows_message_when_empty(paths, capsys) -> None:
    rc = cmd_mapping_list(_list_args(), paths=paths)
    assert rc == 0
    assert "(no mappings)" in capsys.readouterr().out


def test_mapping_list_prints_header_and_rows(paths, capsys) -> None:
    mapping_id = _add_and_get_id(
        paths, source="ibkr", instrument="EMIM", category="Equities / EM / Asia"
    )
    capsys.readouterr()

    rc = cmd_mapping_list(_list_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ID" in out and "SOURCE" in out and "INSTRUMENT" in out and "CATEGORY" in out
    assert str(mapping_id) in out
    assert "EMIM" in out
    assert "Equities / EM / Asia" in out
    assert "1.0000" in out


def test_mapping_list_filters_combine(paths, capsys) -> None:
    _seed_instrument(paths, "ibkr", "EMIM")
    _seed_instrument(paths, "ajbell", "SPAG")
    cmd_mapping_add(
        _add_args(source="ibkr", instrument="EMIM", category="Equities / EM / Asia"),
        paths=paths,
    )
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="1.0",
        ),
        paths=paths,
    )
    capsys.readouterr()

    rc = cmd_mapping_list(_list_args(source="ibkr"), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "EMIM" in out
    assert "SPAG" not in out


# ---------------------------------------------------------------------------
# Repository: weight_sum_micros_for_instrument
# ---------------------------------------------------------------------------


def test_weight_sum_micros_handles_zero_and_partial_and_full(paths) -> None:
    instrument_id = _seed_instrument(paths, "ajbell", "SPAG")
    db = Database.connect(paths.db_path)
    try:
        assert weight_sum_micros_for_instrument(db.connection, instrument_id) == 0
    finally:
        db.close()

    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    db = Database.connect(paths.db_path)
    try:
        assert weight_sum_micros_for_instrument(db.connection, instrument_id) == 620_000
    finally:
        db.close()

    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / Europe",
            weight="0.38",
        ),
        paths=paths,
    )
    db = Database.connect(paths.db_path)
    try:
        assert weight_sum_micros_for_instrument(db.connection, instrument_id) == MICROS_SCALE
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Repository: find_instrument_by_natural_key
# ---------------------------------------------------------------------------


def test_find_instrument_by_natural_key_returns_id_or_none(paths) -> None:
    instrument_id = _seed_instrument(paths, "ibkr", "EMIM")
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ibkr")
        assert (
            find_instrument_by_natural_key(
                db.connection, source_id=source_id, instrument_id_text="EMIM"
            )
            == instrument_id
        )
        assert (
            find_instrument_by_natural_key(
                db.connection, source_id=source_id, instrument_id_text="UNKNOWN"
            )
            is None
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Defensive: schema CHECKs still apply on top of CLI validation
# ---------------------------------------------------------------------------


def test_get_mappings_for_instrument_returns_pairs(paths) -> None:
    _seed_instrument(paths, "ajbell", "SPAG")
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / NAM",
            weight="0.62",
        ),
        paths=paths,
    )
    cmd_mapping_add(
        _add_args(
            source="ajbell",
            instrument="SPAG",
            category="Equities / Developed / Europe",
            weight="0.38",
        ),
        paths=paths,
    )
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ajbell")
        instrument_id = find_instrument_by_natural_key(
            db.connection, source_id=source_id, instrument_id_text="SPAG"
        )
        assert instrument_id is not None
        pairs = get_mappings_for_instrument(db.connection, instrument_id)
        weights = sorted(weight for _, weight in pairs)
        assert weights == [380_000, 620_000]
    finally:
        db.close()
