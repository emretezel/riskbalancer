"""
CLI-level tests for `rb plan export` and `rb plan import`.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import sandboxed_paths, write_plan_yaml_to_db
from riskbalancer.cli import cmd_plan_export, cmd_plan_import
from riskbalancer.configuration import CategoryNode
from riskbalancer.db import Database
from riskbalancer.paths import UserPaths
from riskbalancer.repositories import find_user_id, load_plan_tree, plan_exists

# ---------------------------------------------------------------------------
# Fixtures (mirror the tests/test_cli_plan_adjust.py shape)
# ---------------------------------------------------------------------------

EXPECTED_HEADER = "level1,weight1,level2,weight2,volatility,adjustment"


PLAN_YAML = """\
assets:
  - name: Equities
    weight: 0.6
    children:
      - name: NAM
        weight: 0.5
        volatility: 0.18
        adjustment: 1.0
      - name: EMEA
        weight: 0.5
        volatility: 0.18
        adjustment: 0.9
  - name: Bonds
    weight: 0.4
    volatility: 0.07
    adjustment: 1.0
"""


def _paths_for(tmp_path: Path, user: str = "emre") -> UserPaths:
    """Sandbox a UserPaths under `tmp_path`. Delegates to the shared helper."""
    return sandboxed_paths(tmp_path, user=user)


def _seed_plan(paths: UserPaths, yaml_text: str = PLAN_YAML) -> None:
    """Persist `yaml_text` as the user's plan in the sandboxed database."""
    write_plan_yaml_to_db(paths, yaml_text)


def _load_plan(paths: UserPaths) -> list[CategoryNode]:
    """Reload the user's plan from the sandboxed DB."""
    db = Database.connect(paths.db_path)
    try:
        user_id = find_user_id(db.connection, paths.user)
        assert user_id is not None, f"user {paths.user!r} missing from sandbox DB"
        return load_plan_tree(db.connection, user_id)
    finally:
        db.close()


def _plan_fingerprint(
    paths: UserPaths,
) -> list[tuple[tuple[str, ...], float, float, float]]:
    """Stable representation of every leaf (path, weight, vol, adj) for diffs."""
    nodes = _load_plan(paths)

    def walk(
        children: list[CategoryNode],
        prefix: tuple[str, ...],
    ) -> list[tuple[tuple[str, ...], float, float, float]]:
        out: list[tuple[tuple[str, ...], float, float, float]] = []
        for node in children:
            path = prefix + (node.name,)
            if node.children:
                out.extend(walk(node.children, path))
            else:
                out.append((path, node.weight, node.volatility or 0.0, node.adjustment))
        return out

    return walk(nodes, prefix=())


def _plan_exists_in_db(paths: UserPaths) -> bool:
    """True when the sandboxed DB has at least one plan_node row for the user."""
    db = Database.connect(paths.db_path)
    try:
        user_id = find_user_id(db.connection, paths.user)
        return user_id is not None and plan_exists(db.connection, user_id)
    finally:
        db.close()


def _export_args(out: Path | None = None, user: str = "emre") -> argparse.Namespace:
    return argparse.Namespace(user=user, out=out)


def _import_args(
    csv_path: Path,
    *,
    user: str = "emre",
    yes: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(user=user, csv_path=str(csv_path), yes=yes)


def _script(monkeypatch, answers):
    """Wire `builtins.input` to consume from a scripted answer list."""
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(queue))


# ---------------------------------------------------------------------------
# rb plan export
# ---------------------------------------------------------------------------


def test_export_to_stdout_prints_csv(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_export(_export_args(), paths=paths)
    assert rc == 0
    captured = capsys.readouterr().out
    assert captured.splitlines()[0] == EXPECTED_HEADER
    # NAM is a leaf at depth 2: row should start with Equities,0.6,NAM,...
    assert any(line.startswith("Equities,0.6,NAM,") for line in captured.splitlines())


def test_export_to_file_writes_csv(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    out = tmp_path / "exports" / "plan.csv"

    rc = cmd_plan_export(_export_args(out=out), paths=paths)
    assert rc == 0
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith(EXPECTED_HEADER)


def test_export_writes_only_leaves(tmp_path):
    """Branch rows must not appear — only leaves."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    out = tmp_path / "plan.csv"
    cmd_plan_export(_export_args(out=out), paths=paths)
    rows = out.read_text(encoding="utf-8").splitlines()[1:]  # drop header
    assert len(rows) == 3  # NAM, EMEA, Bonds — Equities branch row is absent


def test_export_missing_plan_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    rc = cmd_plan_export(_export_args(), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err


def test_export_missing_user_errors(tmp_path, capsys):
    """With no user resolved, export falls through to a plan-missing error."""
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_export(_export_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err


# ---------------------------------------------------------------------------
# rb plan import
# ---------------------------------------------------------------------------


def test_import_after_export_round_trips(tmp_path, capsys):
    """Export → import (with --yes) reproduces the same plan in the DB.

    With the database-backed plan store, "round trip" means the leaf set
    and per-leaf (weight, vol, adj) come back identical after a full
    CSV export/import cycle.
    """
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    before = _plan_fingerprint(paths)

    csv_path = tmp_path / "plan.csv"
    rc = cmd_plan_export(_export_args(out=csv_path), paths=paths)
    assert rc == 0
    capsys.readouterr()  # discard "Wrote plan CSV to ..." line

    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0

    assert _plan_fingerprint(paths) == before


def test_import_with_decline_leaves_plan_unchanged(tmp_path, monkeypatch):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = _plan_fingerprint(paths)

    csv_path = tmp_path / "plan.csv"
    rc = cmd_plan_export(_export_args(out=csv_path), paths=paths)
    assert rc == 0
    text = csv_path.read_text(encoding="utf-8")
    # Bump every leaf volatility cell to detect a write would change the file.
    csv_path.write_text(text.replace("0.18", "0.20"), encoding="utf-8")

    _script(monkeypatch, ["n"])
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0
    assert _plan_fingerprint(paths) == original


def test_import_with_confirm_writes_changes(tmp_path, monkeypatch):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    csv_path = tmp_path / "plan.csv"
    cmd_plan_export(_export_args(out=csv_path), paths=paths)
    text = csv_path.read_text(encoding="utf-8")
    # Bump NAM's adjustment from 1.0 to 0.85 in the CSV (full-row pattern with
    # the new interleaved header: Equities,0.6,NAM,0.5,0.18,1.0).
    edited = text.replace(
        "Equities,0.6,NAM,0.5,0.18,1.0",
        "Equities,0.6,NAM,0.5,0.18,0.85",
    )
    assert edited != text, "test fixture must actually edit a value"
    csv_path.write_text(edited, encoding="utf-8")

    _script(monkeypatch, ["y"])
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0

    reloaded = _load_plan(paths)
    nam = next(child for child in reloaded[0].children if child.name == "NAM")
    assert nam.adjustment == pytest.approx(0.85)


def test_import_summary_lists_added_removed_changed(tmp_path, monkeypatch, capsys):
    """A non-trivial diff prints added/removed/changed sections."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    # CSV with EMEA dropped, a new APAC sibling added, Bonds adjustment changed.
    csv_text = (
        "level1,weight1,level2,weight2,volatility,adjustment\n"
        "Equities,0.6,NAM,0.5,0.18,1.0\n"
        "Equities,0.6,APAC,0.5,0.18,1.0\n"
        "Bonds,0.4,,,0.07,0.95\n"
    )
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    _script(monkeypatch, ["n"])
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Import summary" in out
    assert "+1 added" in out
    assert "-1 removed" in out
    assert "~1 changed" in out
    assert "Equities / APAC" in out
    assert "Equities / EMEA" in out


def test_import_branch_weight_edit_shows_in_diff(tmp_path, monkeypatch, capsys):
    """Editing a parent's weight shows up as ~ on every leaf under it."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    # Move Equities from 0.6 to 0.7 and Bonds from 0.4 to 0.3 (still sums to 1).
    # Both Equities leaves should be flagged as changed (cumulative weight diff).
    csv_text = (
        "level1,weight1,level2,weight2,volatility,adjustment\n"
        "Equities,0.7,NAM,0.5,0.18,1.0\n"
        "Equities,0.7,EMEA,0.5,0.18,0.9\n"
        "Bonds,0.3,,,0.07,1.0\n"
    )
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    _script(monkeypatch, ["n"])
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    # All three leaves changed because branch weights moved.
    assert "~3 changed" in out


def test_import_into_fresh_user_creates_plan(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(
        "level1,weight1,volatility,adjustment\nEquities,0.6,0.18,1.0\nBonds,0.4,0.07,1.0\n",
        encoding="utf-8",
    )

    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0
    assert _plan_exists_in_db(paths)
    out = capsys.readouterr().out
    assert "create a new plan" in out


def test_import_invalid_csv_returns_two_and_keeps_plan(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = _plan_fingerprint(paths)

    csv_path = tmp_path / "broken.csv"
    csv_path.write_text("garbage,not,a,header\n", encoding="utf-8")

    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 2
    err = capsys.readouterr().err
    assert "plan import failed" in err
    assert _plan_fingerprint(paths) == original


def test_import_invalid_weights_returns_two(tmp_path, capsys):
    """Weights that don't sum to 100% are rejected by the shared validator."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    csv_path = tmp_path / "bad-weights.csv"
    csv_path.write_text(
        "level1,weight1,volatility,adjustment\nA,0.4,0.1,1.0\nB,0.4,0.1,1.0\n",
        encoding="utf-8",
    )
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 2
    err = capsys.readouterr().err
    assert "validation failed" in err


def test_import_conflicting_branch_weight_returns_two(tmp_path, capsys):
    """Two sibling leaves disagreeing on a parent's weight is rejected."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = _plan_fingerprint(paths)

    csv_path = tmp_path / "conflict.csv"
    csv_path.write_text(
        "level1,weight1,level2,weight2,volatility,adjustment\n"
        # Two rows under Equities disagree on Equities' weight (0.6 vs 0.7).
        "Equities,0.6,NAM,0.5,0.18,1.0\n"
        "Equities,0.7,EMEA,0.5,0.18,0.9\n"
        "Bonds,0.4,,,0.07,1.0\n",
        encoding="utf-8",
    )
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 2
    err = capsys.readouterr().err
    assert "conflicting weight" in err
    assert "Equities" in err
    assert _plan_fingerprint(paths) == original


def test_import_missing_file_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    rc = cmd_plan_import(
        _import_args(tmp_path / "does-not-exist.csv", yes=True),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_import_missing_user_errors(tmp_path, capsys):
    """With no user resolved, plan-import surfaces the file-not-found error first."""
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_import(_import_args(tmp_path / "any.csv", user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


# ---------------------------------------------------------------------------
# Round-trip via write_plan_yaml directly (sanity check)
# ---------------------------------------------------------------------------


def test_write_plan_yaml_then_export_then_import_yields_same_plan(tmp_path):
    """Belt-and-braces round trip starting from a CategoryNode tree.

    After the database migration, "same plan" is checked at the
    fingerprint level — the on-disk YAML form no longer exists, so a
    byte-level comparison is meaningless.
    """
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    before = _plan_fingerprint(paths)

    csv_path = tmp_path / "plan.csv"
    cmd_plan_export(_export_args(out=csv_path), paths=paths)
    cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)

    assert _plan_fingerprint(paths) == before


def test_import_prompts_for_missing_leaf_vol_adj(tmp_path, monkeypatch, capsys):
    """A CSV leaf with an empty volatility cell prompts the user at import.

    The CSV parser tolerates blank vol/adj cells; the historical write
    path raised at write time with a ValueError if the category had no
    recorded vol/adj. Migration 6 keeps the schema-level invariant
    (NULL `category.volatility_micros` blocks plan loading) and the CLI
    now plugs the gap by prompting for the missing values before the
    write happens — the walker has always done this for interactive
    plan creation, and the CSV path now matches.
    """
    paths = _paths_for(tmp_path)
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(
        "level1,weight1,level2,weight2,volatility,adjustment\nEquities,1.0,NAM,1.0,,1.0\n",
        encoding="utf-8",
    )

    # The fill helper asks first for volatility, then for adjustment;
    # the synthetic node's 1.0 adjustment matches the silent default so
    # no suggestion is offered and the user must type both.
    _script(monkeypatch, ["0.20", "1.05"])
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0
    assert _plan_exists_in_db(paths)

    nam = _load_plan(paths)[0].children[0]
    assert nam.name == "NAM"
    assert nam.volatility == pytest.approx(0.20)
    assert nam.adjustment == pytest.approx(1.05)


def test_import_missing_vol_adj_quit_aborts_without_writing(tmp_path, monkeypatch):
    """Typing `quit` at the vol/adj prompt cancels the import cleanly.

    Mirrors the walker's abort semantics: `_ask` translates `quit` (and
    EOF / Ctrl+C) into `PlanCreationAborted`, which the CLI catches and
    reports without touching the DB. No `plan_node` rows must appear.
    """
    paths = _paths_for(tmp_path)
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(
        "level1,weight1,level2,weight2,volatility,adjustment\nEquities,1.0,NAM,1.0,,1.0\n",
        encoding="utf-8",
    )

    _script(monkeypatch, ["quit"])
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 1
    assert not _plan_exists_in_db(paths)


def test_import_reuses_existing_category_attrs_silently(tmp_path, monkeypatch, capsys):
    """If the category already has vol/adj recorded, the import does not prompt.

    Re-importing the same CSV after fundamentals are recorded must be
    silent — the helper looks up the merged columns on `category` and
    fills them onto the in-memory node before any prompt would fire.
    """
    paths = _paths_for(tmp_path)
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(
        "level1,weight1,level2,weight2,volatility,adjustment\nEquities,1.0,NAM,1.0,,1.0\n",
        encoding="utf-8",
    )

    # First import fills the missing vol/adj via the prompt and writes
    # them to category.
    _script(monkeypatch, ["0.22", "1.10"])
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0

    # Second import with the same CSV must not prompt — an empty
    # answers list would raise from ScriptedIO if a prompt fired. We
    # script no answers and rely on _script's KeyError from the empty
    # iter() if that contract were violated.
    _script(monkeypatch, [])
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0

    nam = _load_plan(paths)[0].children[0]
    assert nam.volatility == pytest.approx(0.22)
    assert nam.adjustment == pytest.approx(1.10)
