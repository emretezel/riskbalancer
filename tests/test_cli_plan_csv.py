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
    assert captured.splitlines()[0] == "level1,level2,weight,volatility,adjustment"
    # NAM is a leaf at depth 2 — must show up with its volatility.
    assert any(line.startswith("Equities,NAM,") for line in captured.splitlines())


def test_export_to_file_writes_csv(tmp_path):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    out = tmp_path / "exports" / "plan.csv"

    rc = cmd_plan_export(_export_args(out=out), paths=paths)
    assert rc == 0
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith("level1,level2,weight,volatility,adjustment")


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
    # Canonicalise: load and re-write so the on-disk form matches what
    # `write_plan_yaml` produces (the import path also writes through it).
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

    # Build a CSV with a small edit so we can detect a write would be a change.
    csv_path = tmp_path / "plan.csv"
    rc = cmd_plan_export(_export_args(out=csv_path), paths=paths)
    assert rc == 0
    text = csv_path.read_text(encoding="utf-8")
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
    # Bump NAM's adjustment from 1.0 to 0.85 in the CSV.
    edited = text.replace("Equities,NAM,0.5,0.18,1.0", "Equities,NAM,0.5,0.18,0.85")
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

    # CSV with EMEA dropped, a new "APAC" sibling added, and Bonds adjustment changed.
    # Includes the Equities branch row explicitly (parents must be defined
    # before children are attached) and uses the same root weights as the seed
    # so sibling totals validate cleanly.
    csv_text = (
        "level1,level2,weight,volatility,adjustment\n"
        "Equities,,0.6,,1.0\n"
        "Equities,NAM,0.5,0.18,1.0\n"
        "Equities,APAC,0.5,0.18,1.0\n"
        "Bonds,,0.4,0.07,0.95\n"
    )
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(csv_text, encoding="utf-8")

    _script(monkeypatch, ["n"])  # decline the write — we only care about the summary
    rc = cmd_plan_import(_import_args(csv_path, yes=False), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Import summary" in out
    assert "+1 added" in out
    assert "-1 removed" in out
    # Bonds is "changed" because its adjustment moved from 1.0 to 0.95.
    assert "~1 changed" in out
    assert "Equities / APAC" in out
    assert "Equities / EMEA" in out


def test_import_into_fresh_user_creates_plan(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_path / "plan.csv"
    csv_path.write_text(
        "level1,level2,weight,volatility,adjustment\n"
        "Equities,NAM,1.0,0.18,1.0\n"
        "Bonds,,0.0,0.07,1.0\n",
        encoding="utf-8",
    )
    # Ensure root-level totals to 100% so validation passes (Equities=1.0, Bonds=0.0
    # would fail; rebuild with a valid split).
    csv_path.write_text(
        "level1,weight,volatility,adjustment\nEquities,0.6,0.18,1.0\nBonds,0.4,0.07,1.0\n",
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
        "level1,weight,volatility,adjustment\nA,0.4,0.1,1.0\nB,0.4,0.1,1.0\n",
        encoding="utf-8",
    )
    rc = cmd_plan_import(_import_args(csv_path, yes=True), paths=paths)
    assert rc == 2
    err = capsys.readouterr().err
    assert "validation failed" in err


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
