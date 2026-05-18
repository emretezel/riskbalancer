"""
CLI-level tests for `rb plan adjust`.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import sandboxed_paths, write_plan_yaml_to_db
from riskbalancer.cli import cmd_plan_adjust, cmd_plan_list
from riskbalancer.configuration import CategoryNode
from riskbalancer.db import Database
from riskbalancer.paths import UserPaths
from riskbalancer.plan_adjust import iter_leaf_nodes
from riskbalancer.repositories import find_user_id, load_plan_tree

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
    """Sandbox a UserPaths under tmp_path. Delegates to the shared helper."""
    return sandboxed_paths(tmp_path, user=user)


def _seed_plan(paths: UserPaths, yaml_text: str = PLAN_YAML) -> None:
    """Write `yaml_text` as the user's plan into the sandboxed database."""
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


def _plan_fingerprint(paths: UserPaths) -> list[tuple[tuple[str, ...], float, float, float]]:
    """Stable, comparable summary of every leaf in the user's plan.

    Each tuple is `(path, weight, volatility, adjustment)` — enough detail
    to assert "the plan didn't change" without depending on YAML byte
    equality, which no longer exists once writes go through the DB.
    """
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
    before = _plan_fingerprint(paths)

    rc = cmd_plan_list(_list_args(), paths=paths)

    assert rc == 0
    out = capsys.readouterr().out
    assert "PATH" in out
    assert "Equities / NAM" in out
    assert "Bonds" in out
    # Plan is untouched.
    assert _plan_fingerprint(paths) == before


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
    """With no user resolved, the plan lookup falls through to a plan-missing error."""
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_list(_list_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err


# ---------------------------------------------------------------------------
# Targeted single-leaf set
# ---------------------------------------------------------------------------


def test_targeted_writes_after_confirmation(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    _script(monkeypatch, ["y"])

    rc = cmd_plan_adjust(_args(path="Equities / EMEA", value=0.95), paths=paths)
    assert rc == 0
    reloaded = _load_plan(paths)
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
    reloaded = _load_plan(paths)
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
    original = _plan_fingerprint(paths)
    _script(monkeypatch, ["n"])

    rc = cmd_plan_adjust(_args(path="Equities / EMEA", value=0.95), paths=paths)
    assert rc == 0
    assert _plan_fingerprint(paths) == original


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
    reloaded = _load_plan(paths)
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
    original = _plan_fingerprint(paths)
    _script(monkeypatch, ["0.95", "", "", "n"])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 0
    assert _plan_fingerprint(paths) == original


def test_walker_quit_aborts_with_non_zero_exit(tmp_path, monkeypatch, capsys):
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    original = _plan_fingerprint(paths)
    _script(monkeypatch, ["quit"])

    rc = cmd_plan_adjust(_args(), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "aborted" in err
    assert _plan_fingerprint(paths) == original


def test_walker_under_filters_to_subtree(tmp_path, monkeypatch):
    """`--under "Equities"` restricts prompts to the Equities subtree."""
    paths = _paths_for(tmp_path)
    _seed_plan(paths)
    # Equities has two leaves (NAM, EMEA). Walker should prompt twice.
    _script(monkeypatch, ["", "0.95", "y"])

    rc = cmd_plan_adjust(_args(under="Equities"), paths=paths)
    assert rc == 0
    reloaded = _load_plan(paths)
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
    """With no user resolved, the lookup falls through to a plan-missing error."""
    paths = _paths_for(tmp_path, user="")
    rc = cmd_plan_adjust(_args(user=""), paths=paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err
