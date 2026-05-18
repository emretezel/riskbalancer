"""
Tests for `rb plan create` (clone + empty-catalog paths), `rb plan delete`,
and `rb plan validate`.

The interactive walker (`walk_catalog_interactive`) has its own tests in
the conftest fixtures elsewhere; these focus on the command-level entry
points and the small bits Phase 6 changed (empty-catalog error wording,
clone path, plan delete, plan validate DB-only behaviour).

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from conftest import populate_test_catalog, sandboxed_paths, write_plan_yaml_to_db
from riskbalancer.cli import (
    cmd_plan_create,
    cmd_plan_delete,
    cmd_plan_validate,
)
from riskbalancer.db import Database
from riskbalancer.repositories import (
    find_or_create_user,
    find_user_id,
    load_plan_tree,
    plan_exists,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ALICE_PLAN_YAML = """
assets:
  - name: Equities
    weight: 0.6
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
  - name: Bonds
    weight: 0.4
    children:
      - name: Developed
        weight: 1.0
        children:
          - name: NAM
            weight: 1.0
            volatility: 0.05
            adjustment: 1.0
"""


@pytest.fixture()
def alice_with_plan(tmp_path: Path):
    """A sandbox where user `alice` has a complete, valid plan in the DB."""
    paths = sandboxed_paths(tmp_path, user="alice")
    populate_test_catalog(paths)
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, "alice")
        db.connection.commit()
    finally:
        db.close()
    write_plan_yaml_to_db(paths, ALICE_PLAN_YAML)
    return paths


@pytest.fixture()
def empty_paths(tmp_path: Path):
    """Sandboxed paths with no categories at all."""
    return sandboxed_paths(tmp_path, user="bob")


def _create_args(*, from_user: str | None = None, overwrite: bool = False):
    return argparse.Namespace(from_user=from_user, overwrite=overwrite)


def _delete_args(*, yes: bool = False):
    return argparse.Namespace(yes=yes)


def _validate_args():
    return argparse.Namespace()


# ---------------------------------------------------------------------------
# cmd_plan_create — empty catalog
# ---------------------------------------------------------------------------


def test_plan_create_errors_when_catalog_is_empty(empty_paths, capsys) -> None:
    """An empty `category` table is a hard error with directional guidance."""
    rc = cmd_plan_create(_create_args(), paths=empty_paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No categories defined" in err
    # The new message must mention all three options the user can take.
    assert "rb category add" in err
    assert "rb portfolio import" in err
    assert "rb plan create --from" in err


# ---------------------------------------------------------------------------
# cmd_plan_create — clone path
# ---------------------------------------------------------------------------


def test_plan_create_clones_from_peer(alice_with_plan, tmp_path, capsys) -> None:
    """`--from <peer>` copies the peer's plan tree onto the target user."""
    # Build a sibling sandbox so we can clone alice's plan onto bob.
    bob_paths = sandboxed_paths(tmp_path, user="bob")
    # Reuse alice's DB so the source plan exists on the same connection.
    bob_paths = bob_paths.__class__(
        user="bob",
        root=bob_paths.root,
        user_dir=bob_paths.user_dir,
        statements_dir=bob_paths.statements_dir,
        reports_dir=bob_paths.reports_dir,
        users_root=bob_paths.users_root,
        db_path=alice_with_plan.db_path,
    )

    rc = cmd_plan_create(_create_args(from_user="alice"), paths=bob_paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cloned plan from user 'alice' to 'bob'" in out

    db = Database.connect(bob_paths.db_path)
    try:
        bob_id = find_user_id(db.connection, "bob")
        assert bob_id is not None
        bob_tree = load_plan_tree(db.connection, bob_id)
        # Same structural shape as alice's plan (two top-level branches).
        names = sorted(n.name for n in bob_tree)
        assert names == ["Bonds", "Equities"]
    finally:
        db.close()


def test_plan_create_clone_errors_when_peer_has_no_plan(empty_paths, capsys) -> None:
    """Cloning from a user that doesn't exist surfaces a clear error."""
    populate_test_catalog(empty_paths)
    rc = cmd_plan_create(_create_args(from_user="ghost"), paths=empty_paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Source user 'ghost' has no plan" in err


def test_plan_create_refuses_to_overwrite_existing_plan(alice_with_plan, capsys) -> None:
    """Without `--overwrite`, a second create on the same user errors out."""
    # alice already has a plan from the fixture; try cloning again without overwrite.
    other = sandboxed_paths(alice_with_plan.root, user="alice")
    other = other.__class__(
        user="alice",
        root=other.root,
        user_dir=other.user_dir,
        statements_dir=other.statements_dir,
        reports_dir=other.reports_dir,
        users_root=other.users_root,
        db_path=alice_with_plan.db_path,
    )
    # The clone source must be a different user; reuse alice's plan as a
    # second user "twin" via direct setup.
    db = Database.connect(alice_with_plan.db_path)
    try:
        find_or_create_user(db.connection, "twin")
        db.connection.commit()
    finally:
        db.close()
    write_plan_yaml_to_db(
        alice_with_plan.__class__(
            user="twin",
            root=alice_with_plan.root,
            user_dir=alice_with_plan.users_root / "twin",
            statements_dir=alice_with_plan.users_root / "twin" / "statements",
            reports_dir=alice_with_plan.users_root / "twin" / "reports",
            users_root=alice_with_plan.users_root,
            db_path=alice_with_plan.db_path,
        ),
        ALICE_PLAN_YAML,
    )

    rc = cmd_plan_create(_create_args(from_user="twin", overwrite=False), paths=other)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Plan already exists for user 'alice'" in err
    assert "--overwrite" in err


def test_plan_create_overwrite_replaces_existing_plan(alice_with_plan, capsys) -> None:
    """With `--overwrite`, a clone replaces alice's existing plan."""
    # Set up a "twin" peer with a *different* plan shape so we can observe the swap.
    twin_paths = alice_with_plan.__class__(
        user="twin",
        root=alice_with_plan.root,
        user_dir=alice_with_plan.users_root / "twin",
        statements_dir=alice_with_plan.users_root / "twin" / "statements",
        reports_dir=alice_with_plan.users_root / "twin" / "reports",
        users_root=alice_with_plan.users_root,
        db_path=alice_with_plan.db_path,
    )
    db = Database.connect(alice_with_plan.db_path)
    try:
        find_or_create_user(db.connection, "twin")
        db.connection.commit()
    finally:
        db.close()
    write_plan_yaml_to_db(
        twin_paths,
        """
assets:
  - name: Cash
    weight: 1.0
    children:
      - name: GBP
        weight: 1.0
        volatility: 0.01
        adjustment: 0.0
""",
    )

    rc = cmd_plan_create(_create_args(from_user="twin", overwrite=True), paths=alice_with_plan)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Cloned plan from user 'twin' to 'alice'" in out

    db = Database.connect(alice_with_plan.db_path)
    try:
        alice_id = find_user_id(db.connection, "alice")
        assert alice_id is not None
        alice_tree = load_plan_tree(db.connection, alice_id)
        # alice's plan is now twin's shape: a single top-level `Cash`.
        names = sorted(n.name for n in alice_tree)
        assert names == ["Cash"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_plan_delete
# ---------------------------------------------------------------------------


def test_plan_delete_with_yes_succeeds(alice_with_plan, capsys) -> None:
    rc = cmd_plan_delete(_delete_args(yes=True), paths=alice_with_plan)
    assert rc == 0
    assert "Deleted plan for user 'alice'" in capsys.readouterr().out

    db = Database.connect(alice_with_plan.db_path)
    try:
        alice_id = find_user_id(db.connection, "alice")
        assert alice_id is not None
        # The plan rows are gone but the user row itself survives.
        assert plan_exists(db.connection, alice_id) is False
    finally:
        db.close()


def test_plan_delete_preserves_user_row(alice_with_plan) -> None:
    cmd_plan_delete(_delete_args(yes=True), paths=alice_with_plan)
    db = Database.connect(alice_with_plan.db_path)
    try:
        assert find_user_id(db.connection, "alice") is not None
    finally:
        db.close()


def test_plan_delete_errors_when_no_plan_exists(empty_paths, capsys) -> None:
    """User with no plan rows must error rather than silently no-op."""
    populate_test_catalog(empty_paths)
    db = Database.connect(empty_paths.db_path)
    try:
        find_or_create_user(db.connection, "bob")
        db.connection.commit()
    finally:
        db.close()

    rc = cmd_plan_delete(_delete_args(yes=True), paths=empty_paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "has no plan to delete" in err


def test_plan_delete_errors_when_user_unknown(empty_paths, capsys) -> None:
    rc = cmd_plan_delete(_delete_args(yes=True), paths=empty_paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "has no plan to delete" in err


def test_plan_delete_interactive_confirm(alice_with_plan, monkeypatch, capsys) -> None:
    """Without `--yes`, the command prompts y/N and proceeds on 'y'."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    rc = cmd_plan_delete(_delete_args(yes=False), paths=alice_with_plan)
    assert rc == 0

    db = Database.connect(alice_with_plan.db_path)
    try:
        alice_id = find_user_id(db.connection, "alice")
        assert alice_id is not None
        assert plan_exists(db.connection, alice_id) is False
    finally:
        db.close()


def test_plan_delete_interactive_decline(alice_with_plan, monkeypatch, capsys) -> None:
    """Declining the prompt aborts cleanly with exit code 0 and no DB change."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = cmd_plan_delete(_delete_args(yes=False), paths=alice_with_plan)
    assert rc == 0
    assert "user declined" in capsys.readouterr().out

    # Plan must still be in place.
    db = Database.connect(alice_with_plan.db_path)
    try:
        alice_id = find_user_id(db.connection, "alice")
        assert alice_id is not None
        assert plan_exists(db.connection, alice_id) is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# cmd_plan_validate — DB-only after Phase 1 dropped the YAML --path option
# ---------------------------------------------------------------------------


def test_plan_validate_passes_on_valid_plan(alice_with_plan, capsys) -> None:
    rc = cmd_plan_validate(_validate_args(), paths=alice_with_plan)
    assert rc == 0
    assert "is valid" in capsys.readouterr().out


def test_plan_validate_errors_when_user_has_no_plan(empty_paths, capsys) -> None:
    """No plan → exit 1 with a "run plan create" hint."""
    populate_test_catalog(empty_paths)
    db = Database.connect(empty_paths.db_path)
    try:
        find_or_create_user(db.connection, "bob")
        db.connection.commit()
    finally:
        db.close()

    rc = cmd_plan_validate(_validate_args(), paths=empty_paths)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No plan found" in err
    assert "plan create" in err


def test_plan_validate_rejects_path_argument(alice_with_plan) -> None:
    """The argparse layer must not accept --path any more (Phase 1 dropped it)."""
    from riskbalancer.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["plan", "validate", "--user", "alice", "--path", "anything"])
