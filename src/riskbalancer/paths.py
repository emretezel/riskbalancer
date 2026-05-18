"""
Filesystem path resolution for riskbalancer.

Owns every decision about where on disk a given user's data lives. The DB
(`private/riskbalancer.db`) holds every mutable concept; the filesystem
keeps only raw broker statements (so they can be re-parsed if an adapter
changes) and generated CSV reports.

The on-disk layout is now:

    private/                         (gitignored)
        riskbalancer.db              # authoritative working store
        users/<user>/
            statements/<adapter>/<account>/<year>/...
            reports/<YYYY-MM-DD>.csv

Author: Emre Tezel
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class UserPaths:
    """Every per-user filesystem decision, computed once and passed around.

    Constructed via `for_user(user, root=...)`. Tests build instances by
    pointing `root` at a `tmp_path`, which sandboxes the layout under that
    directory.
    """

    user: str
    root: Path
    user_dir: Path
    statements_dir: Path
    reports_dir: Path
    users_root: Path
    db_path: Path

    @classmethod
    def for_user(cls, user: str, *, root: Optional[Path] = None) -> "UserPaths":
        """Build a `UserPaths` for the given user.

        `root` defaults to `Path(".")` so the resulting paths are relative
        to the current working directory (the project root), matching how
        the CLI is invoked in production. Tests pass a `tmp_path` to
        sandbox filesystem effects.
        """
        base = root if root is not None else Path(".")
        users_root = base / "private" / "users"
        user_dir = users_root / user
        return cls(
            user=user,
            root=base,
            user_dir=user_dir,
            statements_dir=user_dir / "statements",
            reports_dir=user_dir / "reports",
            users_root=users_root,
            db_path=base / "private" / "riskbalancer.db",
        )
