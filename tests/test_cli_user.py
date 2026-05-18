"""
Tests for `riskbalancer user create`, `user list`, and `user delete`.

These commands are the DB-backed user lifecycle. They both insert/delete
rows in the `user` table and manage the on-disk directory that holds the
user's statements and reports.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import sandboxed_paths
from riskbalancer.cli import cmd_user_create, cmd_user_delete, cmd_user_list
from riskbalancer.db import Database
from riskbalancer.paths import UserPaths
from riskbalancer.repositories import find_or_create_user, find_user_id, list_user_names


def test_cmd_user_create_makes_user_dir_and_db_row(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    assert not paths.user_dir.exists()

    result = cmd_user_create(argparse.Namespace(), paths=paths)

    assert result == 0
    assert paths.user_dir.is_dir()
    db = Database.connect(paths.db_path)
    try:
        assert find_user_id(db.connection, "alice") is not None
    finally:
        db.close()
    captured = capsys.readouterr()
    assert "alice" in captured.out
    # The hint should point at `plan create` so the user knows what to do next.
    assert "plan create" in captured.out


def test_cmd_user_create_refuses_when_db_row_exists(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    # First creation succeeds.
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    capsys.readouterr()  # drop initial output

    # Second creation refuses cleanly.
    result = cmd_user_create(argparse.Namespace(), paths=paths)
    assert result == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err


def test_cmd_user_create_rejects_empty_user(tmp_path: Path) -> None:
    """An empty user name reaches the DB and is rejected by the schema CHECK."""
    paths = replace(
        UserPaths.for_user(""),
        users_root=tmp_path / "private" / "users",
        db_path=tmp_path / "riskbalancer.db",
    )
    assert paths.user == ""

    # The DB's `length(name) > 0` CHECK rejects the empty string. The argparse
    # layer enforces --user is non-empty in production; this test exercises
    # the belt-and-braces guard at the schema level.
    with pytest.raises(sqlite3.IntegrityError):
        cmd_user_create(argparse.Namespace(), paths=paths)


def test_cmd_user_list_includes_user_without_plan(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, "alice")
    finally:
        db.close()

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "(no plan yet)" in captured.out


def test_cmd_user_list_reports_leaf_count_once_plan_exists(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    from conftest import write_plan_yaml_to_db

    write_plan_yaml_to_db(
        paths,
        """
assets:
  - name: Equities
    weight: 0.6
    volatility: 0.18
  - name: Bonds
    weight: 0.4
    volatility: 0.05
""".strip(),
    )

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "plan_leaves=2" in captured.out
    assert "(no plan yet)" not in captured.out


def test_cmd_user_list_no_users_root(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    # No user inserted; the DB exists (opened by `cmd_user_list`) but is empty.

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "No stored users." in captured.out


def test_cmd_user_delete_removes_db_row_and_directory(tmp_path: Path, capsys) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    capsys.readouterr()

    args = argparse.Namespace(confirm=True)
    result = cmd_user_delete(args, paths=paths)

    assert result == 0
    assert not paths.user_dir.exists()
    db = Database.connect(paths.db_path)
    try:
        assert find_user_id(db.connection, "alice") is None
        assert "alice" not in list_user_names(db.connection)
    finally:
        db.close()


def test_cmd_user_create_seeds_statements_and_reports_subdirs(tmp_path: Path) -> None:
    """The user dir should ship with `statements/` and `reports/` from day one."""
    paths = sandboxed_paths(tmp_path, user="alice")

    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    assert paths.statements_dir.is_dir()
    assert paths.reports_dir.is_dir()


def test_cmd_user_delete_requires_confirm_flag(tmp_path: Path) -> None:
    paths = sandboxed_paths(tmp_path, user="alice")
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    # Without --confirm, the command must refuse loudly.
    with pytest.raises(ValueError, match="Refusing to delete"):
        cmd_user_delete(argparse.Namespace(confirm=False), paths=paths)
    # And the user must still exist.
    assert paths.user_dir.exists()
    db = Database.connect(paths.db_path)
    try:
        assert find_user_id(db.connection, "alice") is not None
    finally:
        db.close()


def test_cmd_user_delete_works_on_db_only_user(tmp_path: Path) -> None:
    """A user row without an on-disk dir still deletes cleanly."""
    paths = sandboxed_paths(tmp_path, user="alice")
    db = Database.connect(paths.db_path)
    try:
        find_or_create_user(db.connection, "alice")
    finally:
        db.close()
    assert not paths.user_dir.exists()

    rc = cmd_user_delete(argparse.Namespace(confirm=True), paths=paths)
    assert rc == 0
    db = Database.connect(paths.db_path)
    try:
        assert find_user_id(db.connection, "alice") is None
    finally:
        db.close()


def test_cmd_user_delete_works_on_filesystem_only_user(tmp_path: Path) -> None:
    """An orphan directory without a DB row still deletes cleanly."""
    paths = sandboxed_paths(tmp_path, user="ghost")
    paths.user_dir.mkdir(parents=True)
    # The DB exists but has no `ghost` row.
    Database.connect(paths.db_path).close()

    rc = cmd_user_delete(argparse.Namespace(confirm=True), paths=paths)
    assert rc == 0
    assert not paths.user_dir.exists()


def test_cmd_user_delete_errors_when_nothing_exists(tmp_path: Path) -> None:
    paths = sandboxed_paths(tmp_path, user="nobody")
    Database.connect(paths.db_path).close()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        cmd_user_delete(argparse.Namespace(confirm=True), paths=paths)


# ---------------------------------------------------------------------------
# Orphan directory reporting in `rb user list`
# ---------------------------------------------------------------------------


def test_cmd_user_list_flags_orphan_directories(tmp_path: Path, capsys) -> None:
    """A directory under users_root with no matching DB row is reported."""
    paths = sandboxed_paths(tmp_path, user="alice")
    # alice is real (DB row + directory).
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    # zombie has only a directory.
    (paths.users_root / "zombie").mkdir(parents=True)
    # ghost has only a directory too, alphabetically before zombie.
    (paths.users_root / "ghost").mkdir(parents=True)
    capsys.readouterr()

    rc = cmd_user_list(argparse.Namespace(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    # alice is in the DB listing.
    assert "alice" in out
    # The orphan section names both filesystem-only directories.
    assert "Orphan directories" in out
    assert "ghost" in out
    assert "zombie" in out
    # Ordering: ghost (g < z) appears before zombie in the orphan block.
    assert out.index("ghost") < out.index("zombie")


def test_cmd_user_list_no_orphan_section_when_filesystem_clean(tmp_path: Path, capsys) -> None:
    """When every directory has a DB row, the orphan section is omitted."""
    paths = sandboxed_paths(tmp_path, user="alice")
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    capsys.readouterr()

    rc = cmd_user_list(argparse.Namespace(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Orphan" not in out


def test_cmd_user_list_does_not_remove_orphan_directories(tmp_path: Path, capsys) -> None:
    """Listing is informational only — orphans survive the call."""
    paths = sandboxed_paths(tmp_path, user="alice")
    (paths.users_root / "zombie").mkdir(parents=True)
    Database.connect(paths.db_path).close()
    capsys.readouterr()

    cmd_user_list(argparse.Namespace(), paths=paths)
    assert (paths.users_root / "zombie").is_dir()


def test_cmd_user_list_handles_missing_users_root(tmp_path: Path, capsys) -> None:
    """No users_root on disk yet → listing still works without crashing."""
    paths = sandboxed_paths(tmp_path, user="alice")
    Database.connect(paths.db_path).close()
    # Don't create users_root at all.
    assert not paths.users_root.exists()

    rc = cmd_user_list(argparse.Namespace(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No stored users." in out
    assert "Orphan" not in out


def test_cmd_user_list_ignores_files_under_users_root(tmp_path: Path, capsys) -> None:
    """Stray files (e.g. .DS_Store) must not show up in the orphan list."""
    paths = sandboxed_paths(tmp_path, user="alice")
    assert cmd_user_create(argparse.Namespace(), paths=paths) == 0
    (paths.users_root / ".DS_Store").write_text("garbage", encoding="utf-8")
    capsys.readouterr()

    rc = cmd_user_list(argparse.Namespace(), paths=paths)
    assert rc == 0
    out = capsys.readouterr().out
    assert ".DS_Store" not in out
