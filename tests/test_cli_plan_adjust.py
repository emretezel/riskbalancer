"""
CLI-level tests for `rb plan adjust`.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import pytest

from riskbalancer.cli import cmd_plan_adjust, cmd_plan_list
from riskbalancer.configuration import load_category_nodes_from_yaml
from riskbalancer.paths import UserPaths
from riskbalancer.plan_adjust import iter_leaf_nodes

# ---------------------------------------------------------------------------
# Fixtures
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


PLAN_WITH_CASH_YAML = """\
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
  - name: Cash
    weight: 0.0
    volatility: 0.01
    adjustment: 1.0
"""


def _paths_for(tmp_path: Path, user: str = "emre") -> UserPaths:
    """Sandbox a UserPaths under tmp_path with a per-user plan ready to write."""
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
    """Write `yaml_text` to the user's plan path, creating parents as needed."""
    paths.plan.parent.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text(yaml_text, encoding="utf-8")


def _args(**kwargs) -> argparse.Namespace:
    """Default argparse Namespace matching the `plan adjust` subparser shape."""
    defaults = {
        "user": "emre",
        "path": None,
        "value": None,
        "under": None,
        "yes": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _list_args(user: str = "emre") -> argparse.Namespace:
    """Argparse Namespace matching the `plan list` subparser shape."""
    return argparse.Namespace(user=user)


def _script(monkeypatch, answers):
    """Wire `builtins.input` to consume from a scripted answer list."""
    queue = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(queue))


# ---------------------------------------------------------------------------
# rb plan list
# ---------------------------------------------------------------------------


def test_plan_list_prints_table_and_does_not_write(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    mtime_before = paths.plan.stat().st_mtime_ns

    rc = cmd_plan_list(_list_args(), paths=paths)

    assert rc == 0
    out = capsys.readouterr().out
    assert "PATH" in out
    assert "Equities / NAM" in out
    assert "Bonds" in out
    # File is untouched.
    assert paths.plan.stat().st_mtime_ns == mtime_before


def test_plan_list_includes_zero_weight_leaves(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths, PLAN_WITH_CASH_YAML)

    rc = cmd_plan_list(_list_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cash" in out


def test_plan_list_missing_plan_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    rc = cmd_plan_list(_list_args(), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err
    assert "plan create" in err


def test_plan_list_missing_user_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_list(_list_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No user resolved" in err


# ---------------------------------------------------------------------------
# Targeted single-leaf set
# ---------------------------------------------------------------------------


def test_targeted_writes_after_confirmation(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    _script(monkeypatch, ["y"])

    rc = cmd_plan_adjust(_args(path="Equities / EMEA", value=0.95), paths=paths)
    assert rc == 0
    reloaded = load_category_nodes_from_yaml(paths.plan)
    emea = next(n for p, n in iter_leaf_nodes(reloaded) if p[-1] == "EMEA")
    assert emea.adjustment == pytest.approx(0.95)


def test_targeted_yes_skips_prompt(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    def fail(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("input() must not be called when --yes is set")

    monkeypatch.setattr("builtins.input", fail)

    rc = cmd_plan_adjust(_args(path="Equities / EMEA", value=0.95, yes=True), paths=paths)
    assert rc == 0
    reloaded = load_category_nodes_from_yaml(paths.plan)
    emea = next(n for p, n in iter_leaf_nodes(reloaded) if p[-1] == "EMEA")
    assert emea.adjustment == pytest.approx(0.95)


def test_targeted_accepts_arrow_separator(tmp_path, monkeypatch):
    """`>` is accepted as a synonym for `/` in the positional path."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    _script(monkeypatch, ["y"])

    rc = cmd_plan_adjust(_args(path="Equities > EMEA", value=0.95), paths=paths)
    assert rc == 0


def test_targeted_no_confirm_leaves_plan_unchanged(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = paths.plan.read_text(encoding="utf-8")
    _script(monkeypatch, ["n"])

    rc = cmd_plan_adjust(_args(path="Equities / EMEA", value=0.95), paths=paths)
    assert rc == 0
    assert paths.plan.read_text(encoding="utf-8") == original


def test_targeted_rejects_branch_path(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_adjust(_args(path="Equities", value=0.95), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "branch" in err


def test_targeted_rejects_missing_path(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_adjust(_args(path="Equities / Magic", value=0.95), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown category path" in err


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def test_walker_writes_after_confirmation(tmp_path, monkeypatch):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    # Three eligible leaves: NAM, EMEA, Bonds. Change EMEA only, then `y`.
    _script(monkeypatch, ["", "0.95", "", "y"])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 0
    reloaded = load_category_nodes_from_yaml(paths.plan)
    emea = next(n for p, n in iter_leaf_nodes(reloaded) if p[-1] == "EMEA")
    assert emea.adjustment == pytest.approx(0.95)


def test_walker_skips_cash_in_practice(tmp_path, monkeypatch):
    """A plan with Cash (weight 0) prompts three times, not four."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths, PLAN_WITH_CASH_YAML)
    # Three eligible leaves; Cash must not trigger a prompt. If it did, the
    # script would exhaust after three answers and the test would error out.
    _script(monkeypatch, ["", "", "", "y"])

    # No changes recorded → walker says "No changes." and exits 0 before
    # the final "y" confirm. The dangling "y" stays in the queue, which is
    # fine — we only care that NO Cash prompt was issued.
    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 0


def test_walker_no_changes_returns_zero_silently(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    # Three blanks, no apply prompt.
    _script(monkeypatch, ["", "", ""])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No changes" in out


def test_walker_decline_keeps_plan_unchanged(tmp_path, monkeypatch):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = paths.plan.read_text(encoding="utf-8")
    _script(monkeypatch, ["0.95", "", "", "n"])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 0
    assert paths.plan.read_text(encoding="utf-8") == original


def test_walker_quit_aborts_with_non_zero_exit(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = paths.plan.read_text(encoding="utf-8")
    _script(monkeypatch, ["quit"])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "aborted" in err
    assert paths.plan.read_text(encoding="utf-8") == original


def test_walker_under_filters_to_subtree(tmp_path, monkeypatch):
    """`--under "Equities"` restricts prompts to the Equities subtree."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    # Equities has two leaves (NAM, EMEA). Walker should prompt twice.
    _script(monkeypatch, ["", "0.95", "y"])

    rc = cmd_plan_adjust(_args(under="Equities"), paths=paths)
    assert rc == 0
    reloaded = load_category_nodes_from_yaml(paths.plan)
    bonds = next(n for p, n in iter_leaf_nodes(reloaded) if p[-1] == "Bonds")
    # Bonds is outside the subtree — its adjustment must not have changed.
    assert bonds.adjustment == pytest.approx(1.0)


def test_walker_under_no_match_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_adjust(_args(under="Magic"), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not match any leaf" in err


# ---------------------------------------------------------------------------
# Mutex validation and missing plan
# ---------------------------------------------------------------------------


def test_positional_path_without_value_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_adjust(_args(path="Equities / EMEA"), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "value" in err


def test_under_combined_with_path_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)

    rc = cmd_plan_adjust(
        _args(path="Equities / EMEA", value=0.95, under="Bonds"),
        paths=paths,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "--under" in err


def test_adjust_missing_plan_errors_with_helpful_message(tmp_path, capsys):
    paths = _paths_for(tmp_path)
    # Do not seed the plan.
    rc = cmd_plan_adjust(_args(under="Bonds"), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err
    assert "plan create" in err


def test_adjust_missing_user_errors(tmp_path, capsys):
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_adjust(_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No user resolved" in err
