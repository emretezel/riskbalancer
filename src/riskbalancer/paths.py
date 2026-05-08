"""
Filesystem path resolution for riskbalancer.

Owns every decision about where on disk a given user's data lives. All paths
flow through this module so the CLI does not embed layout decisions in command
handlers, and so test code can substitute a custom layout by constructing a
fresh `UserPaths` rather than monkey-patching module-level constants.

The on-disk layout is:

    config/                          (committed)
        seed_plan.yaml               # catalog floor for the very first user
        riskbalancer.yaml            # holds default_user
        mappings/<adapter>.yaml      # shared adapter mappings
        fx.example.yaml              # FX template

    private/                         (gitignored)
        fx.yaml                      # SHARED FX rates across all users
        inbox/                       # SHARED landing zone for unfiled statements
        users/<user>/
            plan.yaml                # this user's category plan
            portfolio.json           # this user's portfolio snapshot
            mappings/                # per-user override directory
                manual.yaml          # always per-user
                <adapter>.yaml       # optional per-user override of shared file
            statements/<broker>/<account>/<year>/...
            reports/<YYYY-MM-DD>.csv

Author: Emre Tezel
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class UserPaths:
    """Every per-user filesystem decision, computed once and passed around.

    Constructed via `for_user(user, root=...)`. Tests build instances either by
    pointing `root` at a `tmp_path` (which sandboxes the entire layout under
    that directory) or by overriding individual fields with
    `dataclasses.replace`.
    """

    # Identity
    user: str
    root: Path

    # Per-user
    user_dir: Path
    plan: Path
    portfolio: Path
    statements_dir: Path
    reports_dir: Path
    overrides_dir: Path
    manual_mappings: Path

    # Shared (across users)
    fx: Path
    fx_template: Path
    shared_mappings_dir: Path
    seed_plan: Path
    users_root: Path
    riskbalancer_config: Path

    @classmethod
    def for_user(cls, user: str, *, root: Optional[Path] = None) -> "UserPaths":
        """Build a `UserPaths` for the given user.

        `root` defaults to `Path(".")` so the resulting paths are relative to
        the current working directory (the project root), matching how the
        CLI is invoked in production. Tests pass a `tmp_path` to sandbox
        filesystem effects.
        """
        base = root if root is not None else Path(".")
        users_root = base / "private" / "users"
        user_dir = users_root / user
        return cls(
            user=user,
            root=base,
            user_dir=user_dir,
            plan=user_dir / "plan.yaml",
            portfolio=user_dir / "portfolio.json",
            statements_dir=user_dir / "statements",
            reports_dir=user_dir / "reports",
            overrides_dir=user_dir / "mappings",
            manual_mappings=user_dir / "mappings" / "manual.yaml",
            fx=base / "private" / "fx.yaml",
            fx_template=base / "config" / "fx.example.yaml",
            shared_mappings_dir=base / "config" / "mappings",
            seed_plan=base / "config" / "seed_plan.yaml",
            users_root=users_root,
            riskbalancer_config=base / "config" / "riskbalancer.yaml",
        )

    def adapter_mappings_path(self, adapter: str) -> Path:
        """Return the **shared** adapter mappings path."""
        return self.shared_mappings_dir / f"{adapter}.yaml"

    def adapter_overrides_path(self, adapter: str) -> Path:
        """Return the **per-user** adapter mappings override path."""
        return self.overrides_dir / f"{adapter}.yaml"


_USER_ENV_VAR = "RISKBALANCER_USER"


def resolve_default_user(*, root: Optional[Path] = None) -> Optional[str]:
    """Resolve the default user name from environment or `riskbalancer.yaml`.

    Returns None when no default is configured; the CLI then errors and asks
    the caller to pass `--user`. The lookup order is:

    1. `RISKBALANCER_USER` environment variable (highest priority).
    2. `default_user` field in `<root>/config/riskbalancer.yaml`.
    """
    env = os.environ.get(_USER_ENV_VAR)
    if env:
        stripped = env.strip()
        if stripped:
            return stripped

    base = root if root is not None else Path(".")
    config_path = base / "config" / "riskbalancer.yaml"
    if not config_path.exists():
        return None
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return None
    value = data.get("default_user")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
