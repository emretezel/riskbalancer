"""
Tests for `riskbalancer user create`, `user list`, and `user delete`.

These commands are the DB-backed user lifecycle. They both insert/delete
rows in the `user` table and manage the on-disk directory that holds the
user's statements and reports.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

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


def test_cmd_user_create_requires_user(tmp_path: Path, capsys) -> None:
    # Empty user simulates "no --user, no env var, no default_user config".
    paths = replace(
        UserPaths.for_user(""),
        users_root=tmp_path / "private" / "users",
        db_path=tmp_path / "riskbalancer.db",
    )
    assert paths.user == ""

    result = cmd_user_create(argparse.Namespace(), paths=paths)

    assert result == 1
    captured = capsys.readouterr()
    assert "No user resolved" in captured.err
    # Nothing should have been created under the users root.
    assert not paths.users_root.exists()


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
