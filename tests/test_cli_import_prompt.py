"""
Tests for the import-time interactive categorisation flow added in Phase 8.

`rb portfolio import` prompts the user for each unmapped instrument:
they can name an existing leaf category, type `new` to create one
inline, type `skip` to defer, or `quit` to stop. `--non-interactive`
skips the prompt entirely.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import populate_test_catalog, sandboxed_paths
from riskbalancer.cli import cmd_portfolio_import
from riskbalancer.db import Database
from riskbalancer.repositories import (
    find_category_by_path,
    find_instrument_by_natural_key,
    find_or_create_user,
    get_mappings_for_instrument,
    get_source_id,
    list_mappings,
    list_unmapped_instrument_ids,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def paths(tmp_path: Path):
    """Sandbox with a seeded catalog and a user row."""
    p = sandboxed_paths(tmp_path, user="alice")
    populate_test_catalog(p)
    db = Database.connect(p.db_path)
    try:
        find_or_create_user(db.connection, p.user)
        db.connection.commit()
    finally:
        db.close()
    return p


def _write_statement(paths, instrument_text: str = "ACME", value: float = 1000.0) -> Path:
    """Drop a one-line AJ Bell statement under the user dir."""
    statement = paths.user_dir / "statement.csv"
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_text(
        f"Investment,Ticker,Value (£)\nAcme Equity,{instrument_text},{value:.2f}\n",
        encoding="utf-8",
    )
    return statement


def _import_args(
    paths,
    *,
    statement: Path,
    non_interactive: bool = False,
    as_of: str = "2026-05-17",
):
    return argparse.Namespace(
        user=paths.user,
        adapter="ajbell",
        account="dealing",
        statement=str(statement),
        as_of=as_of,
        move=False,
        non_interactive=non_interactive,
    )


def _script(monkeypatch, answers: list[str]) -> None:
    """Feed scripted answers to every `input()` call in sequence."""
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(queue))


def _assert_instrument_mapped_to(
    paths, *, source: str, instrument: str, category_path: str
) -> None:
    db = Database.connect(paths.db_path)
    try:
        source_id = get_source_id(db.connection, source)
        instrument_id = find_instrument_by_natural_key(
            db.connection, source_id=source_id, instrument_id_text=instrument
        )
        assert instrument_id is not None
        category_id = find_category_by_path(db.connection, category_path)
        assert category_id is not None
        mappings = get_mappings_for_instrument(db.connection, instrument_id)
        assert (category_id, 1_000_000) in mappings
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Non-interactive flag
# ---------------------------------------------------------------------------


def test_import_non_interactive_defers_unmapped(paths, capsys) -> None:
    """`--non-interactive` skips the prompt and reports the count at the end."""
    statement = _write_statement(paths)

    rc = cmd_portfolio_import(
        _import_args(paths, statement=statement, non_interactive=True), paths=paths
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 instrument(s) still uncategorised" in out

    db = Database.connect(paths.db_path)
    try:
        from riskbalancer.repositories import find_user_id

        user_id = find_user_id(db.connection, paths.user)
        assert user_id is not None
        unmapped = list_unmapped_instrument_ids(db.connection, user_id=user_id)
        assert len(unmapped) == 1
    finally:
        db.close()


def test_import_no_unmapped_means_no_prompt_and_no_warning(paths, capsys, monkeypatch) -> None:
    """If every imported instrument is already mapped, the prompt never fires."""
    statement = _write_statement(paths, instrument_text="ACME")

    # Pre-map ACME to a leaf before importing so it lands already-categorised.
    from riskbalancer.cli import cmd_instrument_add, cmd_mapping_add

    cmd_instrument_add(
        argparse.Namespace(source="ajbell", id="ACME", description=None),
        paths=paths,
    )
    cmd_mapping_add(
        argparse.Namespace(
            source="ajbell",
            instrument="ACME",
            category="Equities / Developed / NAM",
            weight=None,
        ),
        paths=paths,
    )
    capsys.readouterr()

    # If the prompt fires, `input()` raises StopIteration on the empty script,
    # which would surface as a test failure.
    _script(monkeypatch, [])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "uncategorised" not in out


# ---------------------------------------------------------------------------
# Interactive: existing leaf path
# ---------------------------------------------------------------------------


def test_import_interactive_maps_to_existing_leaf(paths, monkeypatch, capsys) -> None:
    """User types a real leaf path and the mapping lands."""
    statement = _write_statement(paths)
    _script(monkeypatch, ["Equities / Developed / NAM"])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    _assert_instrument_mapped_to(
        paths, source="ajbell", instrument="ACME", category_path="Equities / Developed / NAM"
    )
    out = capsys.readouterr().out
    assert "Categorised 1" in out
    assert "still uncategorised" not in out


def test_import_interactive_re_prompts_on_unknown_path(paths, monkeypatch, capsys) -> None:
    """A typo'd path warns and asks again."""
    statement = _write_statement(paths)
    _script(monkeypatch, ["Made / Up / Path", "Equities / Developed / NAM"])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    captured = capsys.readouterr()
    assert "'Made / Up / Path' not found" in captured.err
    _assert_instrument_mapped_to(
        paths, source="ajbell", instrument="ACME", category_path="Equities / Developed / NAM"
    )


def test_import_interactive_re_prompts_on_branch_target(paths, monkeypatch, capsys) -> None:
    """Mapping to a branch is rejected (schema trigger); re-prompt on the same instrument."""
    statement = _write_statement(paths)
    _script(monkeypatch, ["Equities", "Equities / EM / Asia"])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    captured = capsys.readouterr()
    # The repository's pre-check surfaces a friendly "not a leaf" warning.
    assert "not a leaf" in captured.err
    _assert_instrument_mapped_to(
        paths, source="ajbell", instrument="ACME", category_path="Equities / EM / Asia"
    )


# ---------------------------------------------------------------------------
# Interactive: skip / quit
# ---------------------------------------------------------------------------


def test_import_interactive_skip_leaves_instrument_unmapped(paths, monkeypatch, capsys) -> None:
    statement = _write_statement(paths)
    _script(monkeypatch, ["skip"])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "deferred 1" in out
    assert "1 instrument(s) still uncategorised" in out


def test_import_interactive_blank_means_skip(paths, monkeypatch) -> None:
    """An empty line behaves like 'skip'."""
    statement = _write_statement(paths)
    _script(monkeypatch, [""])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    db = Database.connect(paths.db_path)
    try:
        from riskbalancer.repositories import find_user_id

        user_id = find_user_id(db.connection, paths.user)
        assert user_id is not None
        assert len(list_unmapped_instrument_ids(db.connection, user_id=user_id)) == 1
    finally:
        db.close()


def test_import_interactive_quit_stops_remaining_prompts(paths, monkeypatch, capsys) -> None:
    """`quit` on the first instrument leaves the second one unprompted."""
    # A statement with two unmapped instruments.
    statement = paths.user_dir / "two.csv"
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_text(
        "Investment,Ticker,Value (£)\nFoo Inc,FOO,1000.00\nBar Inc,BAR,2000.00\n",
        encoding="utf-8",
    )
    # Only one answer is queued; if the prompt fires twice, StopIteration trips.
    _script(monkeypatch, ["quit"])

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stopping categorisation" in out
    # Two instruments survived as unmapped.
    db = Database.connect(paths.db_path)
    try:
        from riskbalancer.repositories import find_user_id

        user_id = find_user_id(db.connection, paths.user)
        assert user_id is not None
        assert len(list_unmapped_instrument_ids(db.connection, user_id=user_id)) == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Interactive: create new leaf inline
# ---------------------------------------------------------------------------


def test_import_interactive_creates_new_leaf_under_existing_branch(
    paths, monkeypatch, capsys
) -> None:
    """`new` → parent path + name + vol + adj creates a category and maps to it."""
    statement = _write_statement(paths)
    _script(
        monkeypatch,
        [
            "new",
            "Equities / EM",  # parent path
            "Frontier",  # new leaf name
            "0.25",  # volatility
            "1.0",  # adjustment
        ],
    )

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    db = Database.connect(paths.db_path)
    try:
        new_id = find_category_by_path(db.connection, "Equities / EM / Frontier")
        assert new_id is not None
    finally:
        db.close()
    _assert_instrument_mapped_to(
        paths,
        source="ajbell",
        instrument="ACME",
        category_path="Equities / EM / Frontier",
    )


def test_import_interactive_new_leaf_unknown_parent_re_prompts(paths, monkeypatch, capsys) -> None:
    """A bad parent in the inline-new sub-flow returns to the outer prompt."""
    statement = _write_statement(paths)
    _script(
        monkeypatch,
        [
            "new",
            "Made / Up",  # unknown parent → sub-flow aborts, outer re-prompts
            "Equities / EM / Asia",  # then user picks an existing leaf
        ],
    )

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Parent path 'Made / Up' not found" in captured.err
    _assert_instrument_mapped_to(
        paths,
        source="ajbell",
        instrument="ACME",
        category_path="Equities / EM / Asia",
    )


def test_import_interactive_new_leaf_blocked_when_parent_has_mappings(
    paths, monkeypatch, capsys
) -> None:
    """Adding a child to a category with existing mappings is refused."""
    statement = _write_statement(paths)

    # Pre-create another instrument mapped at `Equities / EM` directly so it has
    # a mapping reference. (`Equities / EM` is a branch in the seeded catalog,
    # but its mapping-leaf trigger would normally block this — so add a
    # transient leaf first to be a valid mapping target, then we can simulate
    # the "parent has mappings" guard via a leaf that has mappings.)
    #
    # Simpler: target `Equities / EM / Asia` directly. Asia is a leaf with no
    # children. After mapping AAA → Asia, attempt to add a new leaf under
    # `Equities / EM / Asia` (parent is Asia) — that should be refused because
    # Asia has a mapping pointing at it.
    from riskbalancer.cli import cmd_instrument_add, cmd_mapping_add

    cmd_instrument_add(argparse.Namespace(source="ajbell", id="AAA", description=None), paths=paths)
    cmd_mapping_add(
        argparse.Namespace(
            source="ajbell",
            instrument="AAA",
            category="Equities / EM / Asia",
            weight=None,
        ),
        paths=paths,
    )
    capsys.readouterr()

    _script(
        monkeypatch,
        [
            "new",
            "Equities / EM / Asia",  # parent has the AAA mapping → refuse
            "Equities / EM / Asia",  # outer re-prompt → user picks Asia as the leaf
        ],
    )

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    captured = capsys.readouterr()
    assert "mapping row(s) pointing at it" in captured.err


def test_import_interactive_new_leaf_invalid_number_aborts_subflow(
    paths, monkeypatch, capsys
) -> None:
    """Non-numeric vol input aborts the new-leaf sub-flow cleanly."""
    statement = _write_statement(paths)
    _script(
        monkeypatch,
        [
            "new",
            "Equities / EM",
            "Frontier",
            "not-a-number",  # bad volatility → sub-flow aborts
            "skip",  # outer re-prompt → defer
        ],
    )

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    captured = capsys.readouterr()
    assert "Invalid numeric input" in captured.err
    # No new category should have been created.
    db = Database.connect(paths.db_path)
    try:
        assert find_category_by_path(db.connection, "Equities / EM / Frontier") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Mixed: multiple instruments
# ---------------------------------------------------------------------------


def test_import_interactive_categorises_each_instrument_independently(
    paths, monkeypatch, capsys
) -> None:
    """Two unmapped instruments: map one, skip the other."""
    statement = paths.user_dir / "two.csv"
    statement.parent.mkdir(parents=True, exist_ok=True)
    statement.write_text(
        "Investment,Ticker,Value (£)\nFoo Inc,FOO,1000.00\nBar Inc,BAR,2000.00\n",
        encoding="utf-8",
    )
    _script(
        monkeypatch,
        ["Equities / Developed / NAM", "skip"],
    )

    rc = cmd_portfolio_import(_import_args(paths, statement=statement), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Categorised 1" in out and "deferred 1" in out

    # BAR is prompted before FOO (alphabetical order from
    # `list_unmapped_instruments_detailed`), so BAR gets the
    # category and FOO is the skipped one.
    db = Database.connect(paths.db_path)
    try:
        all_mappings = list_mappings(db.connection)
        mapped_texts = {m["instrument_id_text"] for m in all_mappings}
        assert "BAR" in mapped_texts
        assert "FOO" not in mapped_texts
    finally:
        db.close()
