"""
Tests for `rb instrument list/add/update/delete` and the underlying
repository accessors.

Instruments are global per `(source_id, instrument_id_text)`. CRUD here
covers the surgical-edit use case; auto-creation during
`rb portfolio import` is exercised separately.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import populate_test_catalog, sandboxed_paths
from riskbalancer.cli import (
    cmd_instrument_add,
    cmd_instrument_delete,
    cmd_instrument_list,
    cmd_instrument_update,
    cmd_mapping_add,
    cmd_portfolio_import,
)
from riskbalancer.db import Database
from riskbalancer.repositories import (
    create_instrument,
    delete_instrument,
    find_instrument_by_natural_key,
    get_instrument_by_id,
    get_source_id,
    list_instruments,
    update_instrument_description,
)


@pytest.fixture()
def paths(tmp_path: Path):
    """Sandboxed paths + a minimal seeded catalog (so mapping tests are usable)."""
    p = sandboxed_paths(tmp_path)
    populate_test_catalog(p)
    return p


def _add_args(*, source: str, instrument_id: str, description: str | None = None):
    return argparse.Namespace(source=source, id=instrument_id, description=description)


def _update_args(*, instrument_id: int, description: str):
    return argparse.Namespace(instrument_id=instrument_id, description=description)


def _delete_args(*, instrument_id: int):
    return argparse.Namespace(instrument_id=instrument_id)


def _list_args(*, source: str | None = None):
    return argparse.Namespace(source=source)


# ---------------------------------------------------------------------------
# Repository: list_instruments
# ---------------------------------------------------------------------------


def test_list_instruments_empty_returns_empty_list(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        assert list_instruments(db.connection) == []
    finally:
        db.close()


def test_list_instruments_orders_by_adapter_then_text(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        ibkr = get_source_id(db.connection, "ibkr")
        ajbell = get_source_id(db.connection, "ajbell")
        create_instrument(
            db.connection, source_id=ajbell, instrument_id_text="SPAG", description=None
        )
        create_instrument(db.connection, source_id=ibkr, instrument_id_text="EMIM", description="X")
        create_instrument(
            db.connection, source_id=ibkr, instrument_id_text="AAPL", description=None
        )
        db.connection.commit()
        rows = list_instruments(db.connection)
        labels = [(r["adapter"], r["instrument_id_text"]) for r in rows]
        # Ordered by adapter (ajbell < ibkr alphabetically), then by text within adapter.
        assert labels == [("ajbell", "SPAG"), ("ibkr", "AAPL"), ("ibkr", "EMIM")]
    finally:
        db.close()


def test_list_instruments_filters_by_adapter(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        ibkr = get_source_id(db.connection, "ibkr")
        ajbell = get_source_id(db.connection, "ajbell")
        create_instrument(
            db.connection, source_id=ibkr, instrument_id_text="EMIM", description=None
        )
        create_instrument(
            db.connection, source_id=ajbell, instrument_id_text="SPAG", description=None
        )
        db.connection.commit()
        rows = list_instruments(db.connection, adapter="ibkr")
        assert {r["instrument_id_text"] for r in rows} == {"EMIM"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Repository: create_instrument
# ---------------------------------------------------------------------------


def test_create_instrument_rejects_empty_text(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ibkr")
        with pytest.raises(ValueError, match="non-empty"):
            create_instrument(
                db.connection,
                source_id=source_id,
                instrument_id_text="   ",
                description=None,
            )
    finally:
        db.close()


def test_create_instrument_rejects_blank_description(paths) -> None:
    """Empty / whitespace descriptions must be rejected up-front; the
    schema CHECK would otherwise reject them less informatively."""
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ibkr")
        with pytest.raises(ValueError, match="non-empty"):
            create_instrument(
                db.connection,
                source_id=source_id,
                instrument_id_text="EMIM",
                description="   ",
            )
    finally:
        db.close()


def test_create_instrument_strips_whitespace(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ibkr")
        new_id = create_instrument(
            db.connection,
            source_id=source_id,
            instrument_id_text="  EMIM  ",
            description="  iShares EM IMI  ",
        )
        db.connection.commit()
        row = get_instrument_by_id(db.connection, new_id)
        assert row is not None
        _, text, description = row
        assert text == "EMIM"
        assert description == "iShares EM IMI"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_instrument_add
# ---------------------------------------------------------------------------


def test_instrument_add_inserts_new_row(paths, capsys) -> None:
    rc = cmd_instrument_add(
        _add_args(source="ibkr", instrument_id="EMIM", description="iShares EM IMI"),
        paths=paths,
    )
    assert rc == 0
    assert "Added instrument" in capsys.readouterr().out

    db = Database.connect(paths.db_path)
    try:
        rows = list_instruments(db.connection, adapter="ibkr")
        assert len(rows) == 1
        assert rows[0]["instrument_id_text"] == "EMIM"
        assert rows[0]["description"] == "iShares EM IMI"
    finally:
        db.close()


def test_instrument_add_accepts_missing_description(paths) -> None:
    rc = cmd_instrument_add(_add_args(source="ibkr", instrument_id="EMIM"), paths=paths)
    assert rc == 0
    db = Database.connect(paths.db_path)
    try:
        rows = list_instruments(db.connection, adapter="ibkr")
        assert rows[0]["description"] is None
    finally:
        db.close()


def test_instrument_add_rejects_duplicate_natural_key(paths, capsys) -> None:
    assert cmd_instrument_add(_add_args(source="ibkr", instrument_id="EMIM"), paths=paths) == 0
    capsys.readouterr()

    rc = cmd_instrument_add(_add_args(source="ibkr", instrument_id="EMIM"), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "rb instrument update" in err


def test_instrument_add_same_text_different_source_is_allowed(paths) -> None:
    """Same ticker at two brokers must coexist — they are different rows."""
    assert cmd_instrument_add(_add_args(source="ibkr", instrument_id="EMIM"), paths=paths) == 0
    assert cmd_instrument_add(_add_args(source="ajbell", instrument_id="EMIM"), paths=paths) == 0

    db = Database.connect(paths.db_path)
    try:
        rows = list_instruments(db.connection)
        adapters = {r["adapter"] for r in rows if r["instrument_id_text"] == "EMIM"}
        assert adapters == {"ibkr", "ajbell"}
    finally:
        db.close()


def test_instrument_add_rejects_blank_description(paths, capsys) -> None:
    rc = cmd_instrument_add(
        _add_args(source="ibkr", instrument_id="EMIM", description="   "),
        paths=paths,
    )
    assert rc == 1
    assert "non-empty" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_instrument_update
# ---------------------------------------------------------------------------


def _add_and_get_id(
    paths, *, source: str, instrument_id: str, description: str | None = None
) -> int:
    cmd_instrument_add(
        _add_args(source=source, instrument_id=instrument_id, description=description),
        paths=paths,
    )
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, source)
        result = find_instrument_by_natural_key(
            db.connection, source_id=source_id, instrument_id_text=instrument_id
        )
        assert result is not None
        return result
    finally:
        db.close()


def test_instrument_update_changes_description(paths, capsys) -> None:
    instrument_id = _add_and_get_id(paths, source="ibkr", instrument_id="EMIM")
    capsys.readouterr()

    rc = cmd_instrument_update(
        _update_args(instrument_id=instrument_id, description="iShares Core MSCI EM IMI"),
        paths=paths,
    )
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        row = get_instrument_by_id(db.connection, instrument_id)
        assert row is not None
        _, _, description = row
        assert description == "iShares Core MSCI EM IMI"
    finally:
        db.close()


def test_instrument_update_rejects_unknown_id(paths, capsys) -> None:
    rc = cmd_instrument_update(_update_args(instrument_id=999, description="x"), paths=paths)
    assert rc == 1
    assert "no instrument with id 999" in capsys.readouterr().err


def test_instrument_update_rejects_blank_description(paths, capsys) -> None:
    instrument_id = _add_and_get_id(paths, source="ibkr", instrument_id="EMIM")
    capsys.readouterr()

    rc = cmd_instrument_update(
        _update_args(instrument_id=instrument_id, description="   "), paths=paths
    )
    assert rc == 1
    assert "non-empty" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_instrument_delete
# ---------------------------------------------------------------------------


def test_instrument_delete_removes_unreferenced_row(paths, capsys) -> None:
    instrument_id = _add_and_get_id(paths, source="ibkr", instrument_id="EMIM")
    capsys.readouterr()

    rc = cmd_instrument_delete(_delete_args(instrument_id=instrument_id), paths=paths)
    assert rc == 0

    db = Database.connect(paths.db_path)
    try:
        assert get_instrument_by_id(db.connection, instrument_id) is None
    finally:
        db.close()


def test_instrument_delete_rejects_unknown_id(paths, capsys) -> None:
    rc = cmd_instrument_delete(_delete_args(instrument_id=999), paths=paths)
    assert rc == 1
    assert "no instrument with id 999" in capsys.readouterr().err


def test_instrument_delete_blocked_when_mapping_references_it(paths, capsys) -> None:
    """`mapping.instrument_id REFERENCES instrument(id) ON DELETE RESTRICT`."""
    instrument_id = _add_and_get_id(paths, source="ibkr", instrument_id="EMIM")
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

    rc = cmd_instrument_delete(_delete_args(instrument_id=instrument_id), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "still referenced" in err
    assert "rb mapping delete" in err

    # The instrument row must still be present (the RESTRICT rolled the delete back).
    db = Database.connect(paths.db_path)
    try:
        assert get_instrument_by_id(db.connection, instrument_id) is not None
    finally:
        db.close()


def test_instrument_delete_blocked_when_position_references_it(paths, capsys) -> None:
    """`position.instrument_id REFERENCES instrument(id) ON DELETE RESTRICT`.

    Build the position chain end-to-end via `rb portfolio import` so the
    test sets up the same shape production code does.
    """
    # Need a user before we can import.
    from riskbalancer.repositories import find_or_create_user

    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, paths.user)
        db.connection.commit()
    finally:
        db.close()

    # Synthesise a minimal AJ Bell statement with one holding.
    statement = paths.user_dir / "tiny.csv"
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_text(
        "Investment,Ticker,Value (£)\nAcme Equity,ACME,1000.00\n",
        encoding="utf-8",
    )
    import_rc = cmd_portfolio_import(
        argparse.Namespace(
            user=paths.user,
            adapter="ajbell",
            account="dealing",
            statement=str(statement),
            as_of="2026-05-17",
            move=False,
        ),
        paths=paths,
    )
    assert import_rc == 0
    capsys.readouterr()

    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, "ajbell")
        instrument_id = find_instrument_by_natural_key(
            db.connection, source_id=source_id, instrument_id_text="ACME"
        )
        assert instrument_id is not None
    finally:
        db.close()

    rc = cmd_instrument_delete(_delete_args(instrument_id=instrument_id), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "still referenced" in err

    db = Database.connect(paths.db_path)
    try:
        assert get_instrument_by_id(db.connection, instrument_id) is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_instrument_list
# ---------------------------------------------------------------------------


def test_instrument_list_prints_no_rows_message_when_empty(paths, capsys) -> None:
    rc = cmd_instrument_list(_list_args(), paths=paths)
    assert rc == 0
    assert "(no instruments)" in capsys.readouterr().out


def test_instrument_list_prints_header_and_rows(paths, capsys) -> None:
    instrument_id = _add_and_get_id(
        paths, source="ibkr", instrument_id="EMIM", description="iShares EM IMI"
    )
    capsys.readouterr()

    rc = cmd_instrument_list(_list_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ID" in out and "SOURCE" in out and "INSTRUMENT" in out and "DESCRIPTION" in out
    assert str(instrument_id) in out
    assert "EMIM" in out
    assert "iShares EM IMI" in out


def test_instrument_list_shows_em_dash_for_missing_description(paths, capsys) -> None:
    _add_and_get_id(paths, source="ibkr", instrument_id="EMIM")
    capsys.readouterr()

    cmd_instrument_list(_list_args(), paths=paths)
    out = capsys.readouterr().out
    assert "—" in out


def test_instrument_list_filter_by_source(paths, capsys) -> None:
    cmd_instrument_add(_add_args(source="ibkr", instrument_id="EMIM"), paths=paths)
    cmd_instrument_add(_add_args(source="ajbell", instrument_id="SPAG"), paths=paths)
    capsys.readouterr()

    cmd_instrument_list(_list_args(source="ibkr"), paths=paths)
    out = capsys.readouterr().out
    assert "EMIM" in out
    assert "SPAG" not in out


# ---------------------------------------------------------------------------
# Repository: update_instrument_description / delete_instrument direct calls
# ---------------------------------------------------------------------------


def test_update_instrument_description_clears_when_none(paths) -> None:
    """Passing `None` clears the description column (NULL is a valid state)."""
    instrument_id = _add_and_get_id(
        paths, source="ibkr", instrument_id="EMIM", description="initial"
    )
    db = Database.connect(paths.db_path)
    try:
        update_instrument_description(db.connection, instrument_id=instrument_id, description=None)
        db.connection.commit()
        row = get_instrument_by_id(db.connection, instrument_id)
        assert row is not None
        _, _, description = row
        assert description is None
    finally:
        db.close()


def test_delete_instrument_returns_false_on_missing(paths) -> None:
    db = Database.connect(paths.db_path)
    try:
        assert delete_instrument(db.connection, 12345) is False
    finally:
        db.close()
