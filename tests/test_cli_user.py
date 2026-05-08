"""
Tests for `riskbalancer user create` and the `riskbalancer user list` display.

Author: Emre Tezel
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from riskbalancer.cli import cmd_user_create, cmd_user_list
from riskbalancer.paths import UserPaths


def _paths_for(tmp_path: Path, user: str) -> UserPaths:
    """Build a tmp_path-rooted `UserPaths` for `user`.

    Tests must not touch the real `private/` tree, so we redirect both
    `users_root` and the per-user paths underneath `tmp_path`.
    """
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


def test_cmd_user_create_makes_user_dir(tmp_path: Path, capsys) -> None:
    paths = _paths_for(tmp_path, "alice")
    assert not paths.user_dir.exists()

    result = cmd_user_create(argparse.Namespace(), paths=paths)

    assert result == 0
    assert paths.user_dir.is_dir()
    captured = capsys.readouterr()
    assert str(paths.user_dir) in captured.out
    # The hint should point at `plan create` so the user knows what to do next.
    assert "plan create" in captured.out
    assert "alice" in captured.out


def test_cmd_user_create_refuses_when_dir_exists(tmp_path: Path, capsys) -> None:
    paths = _paths_for(tmp_path, "alice")
    paths.user_dir.mkdir(parents=True)
    # Drop a sentinel file so we can confirm we do not clobber existing state.
    sentinel = paths.user_dir / "plan.yaml"
    sentinel.write_text("assets: []\n", encoding="utf-8")

    result = cmd_user_create(argparse.Namespace(), paths=paths)

    assert result == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    # File untouched.
    assert sentinel.read_text(encoding="utf-8") == "assets: []\n"


def test_cmd_user_create_requires_user(tmp_path: Path, capsys) -> None:
    # Empty user simulates "no --user, no env var, no default_user config".
    paths = replace(
        UserPaths.for_user(""),
        users_root=tmp_path / "private" / "users",
    )
    assert paths.user == ""

    result = cmd_user_create(argparse.Namespace(), paths=paths)

    assert result == 1
    captured = capsys.readouterr()
    assert "No user resolved" in captured.err
    # Nothing should have been created under the users root.
    assert not paths.users_root.exists()


def test_cmd_user_list_includes_user_without_portfolio(tmp_path: Path, capsys) -> None:
    paths = _paths_for(tmp_path, "alice")
    # Two users: `alice` has only a directory, `bob` has nothing yet (control).
    paths.user_dir.mkdir(parents=True)

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "(no portfolio yet)" in captured.out


def test_cmd_user_list_still_shows_portfolio_metadata(tmp_path: Path, capsys) -> None:
    paths = _paths_for(tmp_path, "alice")
    paths.user_dir.mkdir(parents=True)
    paths.portfolio.write_text(
        json.dumps(
            {
                "created_at": "2026-05-01",
                "plan": "plan.yaml",
                "investments": [],
            }
        ),
        encoding="utf-8",
    )

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "plan=plan.yaml" in captured.out
    assert "created=2026-05-01" in captured.out
    assert "(no portfolio yet)" not in captured.out


def test_cmd_user_list_no_users_root(tmp_path: Path, capsys) -> None:
    # Regression for the early-return path when `private/users/` doesn't exist.
    paths = _paths_for(tmp_path, "alice")
    assert not paths.users_root.exists()

    result = cmd_user_list(argparse.Namespace(), paths=paths)

    assert result == 0
    captured = capsys.readouterr()
    assert "No stored users." in captured.out
