"""
Shared pytest configuration and helpers for the RiskBalancer test suite.

Two responsibilities:

1. Put the project's `src/` on `sys.path` so the tests can `import riskbalancer`
   without having to install the package first.
2. Provide helpers that every CLI test depends on:
   - `sandboxed_paths` — a `UserPaths` whose every on-disk and database
     location lands inside `tmp_path`. Tests that exercise commands which
     touch the database MUST use this so they never write to the real
     `private/riskbalancer.db`.
   - `populate_test_catalog` — build a minimal in-memory category tree
     directly via `repositories` so tests have a base catalog to plan
     against without needing any YAML seed files.
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
from riskbalancer.repositories import (  # noqa: E402
    find_or_create_category,
    find_or_create_user,
    upsert_category_attribute,
    write_plan_tree,
)


def sandboxed_paths(tmp_path: Path, user: str = "emre") -> UserPaths:
    """Build a `UserPaths` whose every on-disk + DB location is under `tmp_path`.

    Use this in every CLI test that exercises commands that touch the
    database. Without it, the test would write to the real
    `private/riskbalancer.db` and pollute developer state.
    """
    users_root = tmp_path / "private" / "users"
    user_dir = users_root / user
    return replace(
        UserPaths.for_user(user),
        root=tmp_path,
        users_root=users_root,
        user_dir=user_dir,
        statements_dir=user_dir / "statements",
        reports_dir=user_dir / "reports",
        db_path=tmp_path / "riskbalancer.db",
    )


# Minimal test catalog. Most tests only need a couple of leaves to plan
# against; the few that need more should hand-build via the same
# repository helpers.
_TEST_CATALOG = [
    # (parent_path, name, volatility, adjustment)
    # Equities
    (("Equities",), "Developed", 0.18, 1.0),
    (("Equities",), "EM", 0.22, 1.0),
    (("Equities", "Developed"), "NAM", 0.17, 1.0),
    (("Equities", "Developed"), "Europe", 0.18, 1.0),
    (("Equities", "EM"), "Asia", 0.22, 1.0),
    # Bonds
    (("Bonds",), "Developed", 0.05, 1.0),
    (("Bonds",), "Inflation", 0.08, 1.35),
    (("Bonds", "Developed"), "NAM", 0.05, 1.0),
    (("Bonds", "Developed"), "UK", 0.05, 1.0),
    # Cash
    (("Cash",), "GBP", 0.01, 0.0),
]


def populate_test_catalog(paths: UserPaths) -> None:
    """Seed a minimal category tree into the sandboxed DB.

    Idempotent — re-running it just re-finds the existing rows. Most tests
    that need "a category exists" should call this from a fixture rather
    than constructing rows by hand.
    """
    db = Database.connect(paths.db_path)
    try:
        # Resolve parent ids by walking the prefix tuples.
        parent_cache: dict[tuple[str, ...], int] = {}
        for parent_path, name, vol, adj in _TEST_CATALOG:
            # Resolve every ancestor first so child inserts have a parent_id.
            current_parent_id = None
            running: list[str] = []
            for segment in parent_path:
                running.append(segment)
                key = tuple(running)
                if key in parent_cache:
                    current_parent_id = parent_cache[key]
                else:
                    current_parent_id = find_or_create_category(
                        db.connection,
                        parent_id=current_parent_id,
                        name=segment,
                    )
                    parent_cache[key] = current_parent_id
            leaf_id = find_or_create_category(
                db.connection,
                parent_id=current_parent_id,
                name=name,
            )
            upsert_category_attribute(
                db.connection,
                category_id=leaf_id,
                volatility=vol,
                adjustment=adj,
            )
            parent_cache[tuple(list(parent_path) + [name])] = leaf_id
        db.connection.commit()
    finally:
        db.close()


def write_plan_yaml_to_db(paths: UserPaths, yaml_text: str) -> None:
    """Parse a plan YAML blob and write the resulting tree to the database.

    Historical tests describe plans inline as YAML strings (the legacy
    on-disk format). This bridge keeps those fixtures usable without
    rewriting every test to construct `CategoryNode` trees by hand. The
    referenced categories are auto-created in the DB on the fly.
    """
    paths.user_dir.mkdir(parents=True, exist_ok=True)
    temp_plan = paths.user_dir / "_plan_fixture.yaml"
    temp_plan.write_text(yaml_text, encoding="utf-8")
    try:
        nodes = load_category_nodes_from_yaml(temp_plan)
    finally:
        temp_plan.unlink()

    db = Database.connect(paths.db_path)
    try:
        user_id = find_or_create_user(db.connection, paths.user)
        write_plan_tree(db.connection, user_id, nodes)
    finally:
        db.close()
