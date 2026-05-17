"""
Seed loader: ingest the committed YAML catalog into the database.

`config/seed_plan.yaml` and every `config/mappings/<adapter>.yaml` file are
authored by hand and committed to the repo. They serve as one-shot seed
inputs that prime the database. `seed_from_yaml()` is idempotent: re-running
it after the YAML changes brings the DB back in sync with the files (YAML
is authoritative for the mapping table; per-user state is never touched).

What this module does NOT do:
- Touch per-user state (plans, statement imports, positions). Those live
  in the database only and are out of scope for the seed loader.
- Validate sibling weights sum to 1.0. The seed plan is presumed to come
  from `plan validate`-clean YAML; the seed loader trusts what it reads
  and is purely structural.

Author: Emre Tezel
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

# Public constant: convert a [0, 1] fraction (or unbounded multiplier like
# `adjustment`) to integer parts-per-million for storage. Rounded so a
# user's `0.62 + 0.05 + 0.13 + 0.2` sums back to exactly 1_000_000 instead
# of 999_999.
MICROS_SCALE = 1_000_000


def fraction_to_micros(value: float) -> int:
    """Round a real-valued fraction to integer parts-per-million.

    Used wherever the schema expects a `*_micros` column (weights,
    volatility, adjustment, FX rates). Range validation is the database's
    job via `CHECK` constraints — this helper is pure arithmetic.
    """
    return round(value * MICROS_SCALE)


def seed_from_yaml(
    connection: sqlite3.Connection,
    *,
    seed_plan_path: Path,
    mappings_dir: Path,
) -> None:
    """Load the committed YAML catalog into the DB. Idempotent.

    Runs inside a single transaction so a partial failure leaves the
    database in its prior state. Behaviour:

    - `seed_plan_path` is walked depth-first; every category node becomes
      (or matches) a row in `category`. Seed leaves additionally fill in
      the merged `volatility_micros` / `adjustment_micros` columns on
      that `category` row (migration 6). Seed branches leave those
      columns NULL — branch-level vol/adj is not a fact the schema
      holds; a user who wants to hold a branch as a plan-leaf must
      supply explicit vol/adj at plan-creation time. Categories that
      already exist are kept as-is; rows are never deleted.
    - For each `<adapter>.yaml` in `mappings_dir`, every mapping row for
      that adapter is deleted up-front (so removed allocations actually
      go away on re-seed) and replaced with the file's contents. The
      mapping table is global — there is no per-user scope.
    """
    connection.execute("BEGIN")
    try:
        _seed_categories_from_plan(connection, seed_plan_path)
        if mappings_dir.exists():
            for mapping_file in sorted(mappings_dir.glob("*.yaml")):
                _seed_mappings_for_adapter(connection, mapping_file.stem, mapping_file)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def _seed_categories_from_plan(connection: sqlite3.Connection, path: Path) -> None:
    """Walk the seed plan YAML; ensure `category` rows and fill leaf vol/adj."""
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assets = data.get("assets", data) if isinstance(data, dict) else data
    if not isinstance(assets, list):
        return
    for entry in assets:
        if isinstance(entry, dict):
            _walk_seed_node(connection, parent_id=None, payload=entry)


def _walk_seed_node(
    connection: sqlite3.Connection,
    *,
    parent_id: Optional[int],
    payload: Mapping[str, Any],
) -> None:
    """Recursive walker: insert category for `payload`, recurse into children.

    Seed leaves fill in the merged `volatility_micros` /
    `adjustment_micros` columns on `category` with their intrinsic
    fundamentals. Seed branches keep those columns NULL — branch-level
    vol/adj is not a fact the schema holds. A leaf with no explicit
    `volatility` in the YAML is silently skipped at the attribute write
    step: its `category` row still exists for the hierarchy, but it
    cannot serve as a plan-leaf until the walker collects vol/adj from
    the user.
    """
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return
    name = raw_name.strip()
    category_id = _find_or_create_category(connection, parent_id=parent_id, name=name)
    children = payload.get("children") or []
    is_branch = isinstance(children, list) and bool(children)
    if is_branch:
        for child in children:
            if isinstance(child, dict):
                _walk_seed_node(connection, parent_id=category_id, payload=child)
        return
    volatility_raw = payload.get("volatility")
    adjustment_raw = payload.get("adjustment", 1.0)
    if volatility_raw is None or adjustment_raw is None:
        # The paired-NULL CHECK on `category` requires both columns
        # together; a seed leaf without an explicit volatility is
        # incomplete data, not a row we silently materialise with
        # placeholders. The walker is responsible for filling it in
        # when (and only when) a user actually adopts the category.
        return
    vol_micros = fraction_to_micros(float(volatility_raw))
    adj_micros = fraction_to_micros(float(adjustment_raw))
    connection.execute(
        """
        UPDATE category
        SET volatility_micros = ?, adjustment_micros = ?
        WHERE id = ?
        """,
        (vol_micros, adj_micros, category_id),
    )


def _find_or_create_category(
    connection: sqlite3.Connection,
    *,
    parent_id: Optional[int],
    name: str,
) -> int:
    """Find or insert `(parent_id, name)` in `category` and return its id.

    Top-level categories (`parent_id IS NULL`) are matched via the partial
    unique index `idx_category_top_level_name`; deeper categories rely on
    the `UNIQUE (parent_id, name)` table constraint.
    """
    if parent_id is None:
        row = connection.execute(
            "SELECT id FROM category WHERE parent_id IS NULL AND name = ?",
            (name,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM category WHERE parent_id = ? AND name = ?",
            (parent_id, name),
        ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (parent_id, name),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to insert category {name!r}")
    return int(cursor.lastrowid)


def resolve_category_path(
    connection: sqlite3.Connection,
    parts: Sequence[str],
) -> int:
    """Resolve a full path (e.g. ('Bonds', 'Developed', 'NAM', 'Govt')) to a row id.

    Missing intermediate or leaf categories are created. This is the only
    sanctioned way to materialise a category path from text — both the
    seed loader and the (later) interactive walker funnel through here so
    the find-or-create semantics stay consistent.
    """
    current: Optional[int] = None
    for raw in parts:
        name = raw.strip()
        if not name:
            raise ValueError("category path contains an empty component")
        current = _find_or_create_category(connection, parent_id=current, name=name)
    if current is None:
        raise ValueError("category path is empty")
    return current


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def _seed_mappings_for_adapter(
    connection: sqlite3.Connection,
    adapter: str,
    path: Path,
) -> None:
    """Replace all mappings for `adapter` with the YAML contents.

    The mapping table is global (no scope, no user_id) — every mapping
    applies to every user. Per-adapter wiping at the start means that
    removing an instrument from the YAML actually removes it on re-seed.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return
    source_id = _resolve_source_id(connection, adapter)
    connection.execute(
        """
        DELETE FROM mapping
        WHERE instrument_id IN (SELECT id FROM instrument WHERE source_id = ?)
        """,
        (source_id,),
    )
    for raw_instrument_id, payload in data.items():
        if not isinstance(payload, dict):
            continue
        allocations = payload.get("allocations")
        if not isinstance(allocations, list) or not allocations:
            continue
        instrument_id_text = str(raw_instrument_id).strip()
        if not instrument_id_text:
            continue
        description_raw = payload.get("description")
        description = str(description_raw).strip() if isinstance(description_raw, str) else None
        instrument_id = _find_or_create_instrument(
            connection,
            source_id=source_id,
            instrument_id_text=instrument_id_text,
            description=description,
        )
        for allocation in allocations:
            if not isinstance(allocation, dict):
                continue
            category_text = str(allocation.get("category", "")).strip()
            if not category_text:
                continue
            parts = tuple(p.strip() for p in category_text.split("/") if p.strip())
            if not parts:
                continue
            weight_raw = allocation.get("weight")
            if weight_raw is None:
                continue
            try:
                weight_micros = fraction_to_micros(float(weight_raw))
            except (TypeError, ValueError):
                continue
            if weight_micros <= 0:
                continue
            category_id = resolve_category_path(connection, parts)
            connection.execute(
                "INSERT INTO mapping (instrument_id, category_id, weight_micros) VALUES (?, ?, ?)",
                (instrument_id, category_id, weight_micros),
            )


def _resolve_source_id(connection: sqlite3.Connection, adapter: str) -> int:
    """Return the surrogate `source.id` for an adapter, or raise.

    Migration 1 pre-populates `source` with one row per known adapter, so
    a missing row here means the adapter name is not in `KNOWN_ADAPTERS`
    — either a typo in the YAML filename or a broker that has not been
    registered yet. We raise rather than auto-create because the CHECK
    on `source.adapter` would reject anything outside `KNOWN_ADAPTERS`
    anyway, and silently inventing rows would mask bugs.
    """
    row = connection.execute(
        "SELECT id FROM source WHERE adapter = ?",
        (adapter,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Unknown adapter {adapter!r}: no row in `source`. "
            f"Add it to `KNOWN_ADAPTERS` in migrations.py if it is a real broker."
        )
    return int(row["id"])


def _find_or_create_instrument(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    instrument_id_text: str,
    description: Optional[str],
) -> int:
    """Find or insert `(source_id, instrument_id_text)` and return its row id.

    The description is updated only when the existing row has no
    description and the new one does — the YAML is the source of truth
    for descriptions on shared instruments, but the loader doesn't
    overwrite a description the user already curated.
    """
    row = connection.execute(
        "SELECT id, description FROM instrument WHERE source_id = ? AND instrument_id_text = ?",
        (source_id, instrument_id_text),
    ).fetchone()
    if row is not None:
        existing_id = int(row["id"])
        if description and not row["description"]:
            connection.execute(
                "UPDATE instrument SET description = ? WHERE id = ?",
                (description, existing_id),
            )
        return existing_id
    cursor = connection.execute(
        "INSERT INTO instrument (source_id, instrument_id_text, description) VALUES (?, ?, ?)",
        (source_id, instrument_id_text, description),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(
            f"Failed to insert instrument (source_id={source_id}, {instrument_id_text!r})"
        )
    return int(cursor.lastrowid)
