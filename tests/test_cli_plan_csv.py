"""
CLI-level tests for `rb plan export` and `rb plan import`.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pytest

from riskbalancer.cli import cmd_plan_export, cmd_plan_import
from riskbalancer.configuration import load_category_nodes_from_yaml
from riskbalancer.paths import UserPaths
from riskbalancer.plan_bootstrap import write_plan_yaml

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
    """Sandbox a UserPaths under `tmp_path` so tests never touch real `private/`."""
    users_root = tmp_path / "private" / "users"
    user_dir = users_root / user
    return replace(
        UserPaths.for_user(user),
        users_root=users_root,
        user_dir=user_dir,
        plan=user_dir / "plan.yaml",
        portfolio=user_dir / "portfolio.json",
        statements_dir=user_dir / "statements",
        reports_dir=user_dir / "reports",
        overrides_dir=user_dir / "mappings",
        manual_mappings=user_dir / "mappings" / "manual.yaml",
    )


def _seed_plan(paths: UserPaths, yaml_text: str = PLAN_YAML) -> None:
    paths.plan.parent.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text(yaml_text, encoding="utf-8")


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
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_export(_export_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No user resolved" in err


# ---------------------------------------------------------------------------
# rb plan import
# ---------------------------------------------------------------------------


def test_import_after_export_round_trips(tmp_path, capsys):
    """Export → import (with --yes) reproduces the canonical plan byte-for-byte.

    Normalises the seed plan through `write_plan_yaml` first so the
    comparison is against the writer's canonical formatting (no extra list
    indent), which is what `cmd_plan_import` will write.
    """
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    canonical_nodes = load_category_nodes_from_yaml(paths.plan)
    write_plan_yaml(paths.plan, canonical_nodes)
    canonical_yaml = paths.plan.read_text(encoding="utf-8")

    csv_path = tmp_path / "plan.csv"
    rc = cmd_plan_export(_export_args(out=csv_path), paths=paths)
    assert rc == 0
    capsys.readouterr()  # discard "Wrote plan CSV to ..." line

    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 0

    assert paths.plan.read_text(encoding="utf-8") == canonical_yaml


def test_import_with_decline_leaves_plan_unchanged(tmp_path, monkeypatch):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original_yaml = paths.plan.read_text(encoding="utf-8")

    csv_path = tmp_path / "plan.csv"
    rc = cmd_plan_export(_export_args(out=csv_path), paths=paths)
    assert rc == 0
    text = csv_path.read_text(encoding="utf-8")
    # Bump every leaf volatility cell to detect a write would change the file.
    csv_path.write_text(text.replace("0.18", "0.20"), encoding="utf-8")

    _script(monkeypatch, ["n"])
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0
    assert paths.plan.read_text(encoding="utf-8") == original_yaml


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

    reloaded = load_category_nodes_from_yaml(paths.plan)
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
    assert paths.plan.exists()
    out = capsys.readouterr().out
    assert "create a new plan" in out


def test_import_invalid_csv_returns_two_and_keeps_plan(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original_yaml = paths.plan.read_text(encoding="utf-8")

    csv_path = tmp_path / "broken.csv"
    csv_path.write_text("garbage,not,a,header\n", encoding="utf-8")

    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 2
    err = capsys.readouterr().err
    assert "plan import failed" in err
    assert paths.plan.read_text(encoding="utf-8") == original_yaml


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
    original_yaml = paths.plan.read_text(encoding="utf-8")

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
    assert paths.plan.read_text(encoding="utf-8") == original_yaml


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
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_import(_import_args(tmp_path / "any.csv", user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No user resolved" in err


# ---------------------------------------------------------------------------
# Round-trip via write_plan_yaml directly (sanity check)
# ---------------------------------------------------------------------------


def test_write_plan_yaml_then_export_then_import_yields_same_yaml(tmp_path):
    """Belt-and-braces round trip starting from a CategoryNode tree."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original_nodes = load_category_nodes_from_yaml(paths.plan)
    canonical_path = tmp_path / "canonical.yaml"
    write_plan_yaml(canonical_path, original_nodes)
    canonical_yaml = canonical_path.read_text(encoding="utf-8")

    csv_path = tmp_path / "plan.csv"
    cmd_plan_export(_export_args(out=csv_path), paths=paths)
    cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)

    assert paths.plan.read_text(encoding="utf-8") == canonical_yaml
