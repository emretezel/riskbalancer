"""
Shared pytest configuration and helpers for the RiskBalancer test suite.

Two responsibilities:

1. Put the project's `src/` on `sys.path` so the tests can `import riskbalancer`
   without having to install the package first.
2. Provide a handful of helpers that every CLI test depends on:
   - `sandboxed_paths` — a `UserPaths` whose every on-disk and database
     location lands inside `tmp_path`. Tests that exercise commands which
     touch the database MUST use this so they never write to the real
     `private/riskbalancer.db`.
   - `seed_test_database` — seed the committed YAML catalog into a
     sandboxed database. Most plan/portfolio tests need the categories
     present before they can write a plan.
   - `write_plan_yaml_to_db` — convert a YAML plan blob into rows in the
     database. The historical test corpus describes plans as YAML strings;
     this helper bridges that to the DB-backed world.

Author: Emre Tezel
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Importing after `sys.path` is set up so the package is resolvable.
from riskbalancer.configuration import load_category_nodes_from_yaml  # noqa: E402
from riskbalancer.db import Database  # noqa: E402
from riskbalancer.paths import UserPaths  # noqa: E402
from riskbalancer.repositories import find_or_create_user, write_plan_tree  # noqa: E402
from riskbalancer.seed import seed_from_yaml  # noqa: E402

# Real seed sources used by tests that need a populated catalog.
_SEED_PLAN_PATH = ROOT / "config" / "seed_plan.yaml"
_SHARED_MAPPINGS_DIR = ROOT / "config" / "mappings"


def sandboxed_paths(tmp_path: Path, user: str = "emre") -> UserPaths:
    """Build a `UserPaths` whose every on-disk + DB location is under `tmp_path`.

    Use this in every CLI test that exercises commands that touch the
    database. Without it, the test would write to the real
    `private/riskbalancer.db` and pollute developer state. The legacy
    per-user fields (`plan`, `portfolio`, `manual_mappings`) are kept so
    existing assertions about file paths still resolve to a path object
    even when no file is actually written there.
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
        db_path=tmp_path / "riskbalancer.db",
    )


def seed_test_database(paths: UserPaths) -> None:
    """Open the sandboxed database and seed it from the committed YAML catalog.

    Called by tests that need to reference seed-defined categories (e.g.
    `Equities`, `Bonds / Developed / NAM / Govt`). The DB file is created
    if missing. Idempotent — re-running it does not multiply rows.
    """
    db = Database.connect(paths.db_path)
    try:
        seed_from_yaml(
            db.connection,
            seed_plan_path=_SEED_PLAN_PATH,
            mappings_dir=_SHARED_MAPPINGS_DIR,
        )
    finally:
        db.close()


def write_plan_yaml_to_db(paths: UserPaths, yaml_text: str) -> None:
    """Parse a plan YAML blob and write the resulting tree to the database.

    Historical tests describe plans inline as YAML strings (the legacy
    on-disk format). This bridge keeps those fixtures usable without
    rewriting every test to construct `CategoryNode` trees by hand.
    Auto-seeds the DB if the catalog is empty so the test's plan can
    reference seed-defined categories.
    """
    # Persist the YAML to a temporary file so we can hand it to the loader.
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    temp_plan = paths.user_dir / "_plan_fixture.yaml"
    temp_plan.write_text(yaml_text, encoding="utf-8")
    try:
        nodes = load_category_nodes_from_yaml(temp_plan)
    finally:
        temp_plan.unlink()

    db = Database.connect(paths.db_path)
    try:
        # Auto-seed on first touch so the categories referenced by the plan
        # have a tree to attach to. Subsequent calls are no-ops.
        from riskbalancer.repositories import find_or_create_user as _foc_user

        _foc_user  # noqa: B018 — silence unused-import without exposing internals
        empty = db.connection.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]
        if empty == 0:
            seed_from_yaml(
                db.connection,
                seed_plan_path=_SEED_PLAN_PATH,
                mappings_dir=_SHARED_MAPPINGS_DIR,
            )
        user_id = find_or_create_user(db.connection, paths.user)
        write_plan_tree(db.connection, user_id, nodes)
    finally:
        db.close()
