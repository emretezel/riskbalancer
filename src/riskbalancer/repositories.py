"""
Repository layer: typed accessors over the RiskBalancer database.

Repositories own all SQL for the project. They take a `sqlite3.Connection`
and return plain Python values or domain dataclasses — callers (CLI
commands, the migration tool, the interactive walkers) never write SQL.

The module is organised into one section per aggregate:

- Users
- Categories (find-or-create, path resolution, suggestion lookups)
- Plans (load/save plan trees as `CategoryNode` lists)
- Mappings (global instrument-to-category, leaf-only)
- Instruments (find-or-create per `(source_id, instrument_id_text)`)
- FX rates (date-keyed historical, plus per-import snapshots)

Author: Emre Tezel
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Iterator, Optional, Sequence

from .configuration import CategoryNode
from .seed import MICROS_SCALE, fraction_to_micros

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.

    Centralised so every `created_at` / `imported_at` row uses the same
    formatting that the CHECK constraints expect.
    """
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def find_user_id(connection: sqlite3.Connection, name: str) -> Optional[int]:
    """Return the user's row id, or None if the name is unknown."""
    row = connection.execute("SELECT id FROM user WHERE name = ?", (name,)).fetchone()
    return int(row["id"]) if row is not None else None


def create_user(connection: sqlite3.Connection, name: str) -> int:
    """Insert a new user and return the row id. Raises on duplicate name."""
    cursor = connection.execute(
        "INSERT INTO user (name, created_at) VALUES (?, ?)",
        (name, _utc_now_iso()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to insert user {name!r}")
    return int(cursor.lastrowid)


def find_or_create_user(connection: sqlite3.Connection, name: str) -> int:
    """Find the user by name, inserting a fresh row when missing."""
    existing = find_user_id(connection, name)
    if existing is not None:
        return existing
    return create_user(connection, name)


def delete_user(connection: sqlite3.Connection, user_id: int) -> None:
    """Delete a user and (via cascading FKs) their plan, sources, accounts,
    statement imports, and positions.

    Wraps the call in a transaction so a partial failure doesn't leave
    half-cleaned state behind. Categories, instruments, and mappings are
    RESTRICTed (or simply global) — those survive the user.
    """
    connection.execute("BEGIN")
    try:
        connection.execute("DELETE FROM user WHERE id = ?", (user_id,))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def list_user_names(connection: sqlite3.Connection) -> list[str]:
    """Return every user name in deterministic alphabetical order."""
    rows = connection.execute("SELECT name FROM user ORDER BY name").fetchall()
    return [row["name"] for row in rows]


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def find_or_create_category(
    connection: sqlite3.Connection,
    *,
    parent_id: Optional[int],
    name: str,
) -> int:
    """Find or insert `(parent_id, name)` in `category` and return its id.

    Top-level lookups (`parent_id IS NULL`) use the partial unique index
    `idx_category_top_level_name`; deeper lookups rely on the
    `UNIQUE (parent_id, name)` constraint. Whitespace in `name` is stripped
    so we don't materialise two distinct rows for what the user typed as
    " NAM " versus "NAM".
    """
    clean = name.strip()
    if not clean:
        raise ValueError("category name must be non-empty")
    if parent_id is None:
        row = connection.execute(
            "SELECT id FROM category WHERE parent_id IS NULL AND name = ?",
            (clean,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM category WHERE parent_id = ? AND name = ?",
            (parent_id, clean),
        ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = connection.execute(
        "INSERT INTO category (parent_id, name) VALUES (?, ?)",
        (parent_id, clean),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to insert category {clean!r}")
    return int(cursor.lastrowid)


def resolve_category_path(
    connection: sqlite3.Connection,
    parts: Sequence[str],
) -> int:
    """Resolve a full path to a `category.id`, creating missing nodes.

    Mirrors `seed.resolve_category_path` — kept here in the repository
    layer so non-seed callers can use it without depending on the seed
    module's side-effect-laden top-level concerns.
    """
    current: Optional[int] = None
    for raw in parts:
        clean = raw.strip()
        if not clean:
            raise ValueError("category path contains an empty component")
        current = find_or_create_category(connection, parent_id=current, name=clean)
    if current is None:
        raise ValueError("category path is empty")
    return current


def get_category_path(connection: sqlite3.Connection, category_id: int) -> str:
    """Return the full ` / `-joined path for the given category id."""
    row = connection.execute(
        "SELECT path FROM category_path WHERE id = ?", (category_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown category id {category_id}")
    return str(row["path"])


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def load_plan_tree(
    connection: sqlite3.Connection,
    user_id: int,
) -> list[CategoryNode]:
    """Reconstruct the user's plan as a `CategoryNode` tree.

    Returns an empty list when the user has no plan rows. Each row's
    weight is read from `plan_node`; volatility and adjustment for
    plan-leaves are read directly from the merged `category` columns
    (migration 6) and raise if a leaf's category has NULL vol/adj — a
    plan should never have been saved without explicit fundamentals on
    every leaf, the walker and CLI import enforce this on creation.
    Branch nodes (those with plan children) carry `None` for volatility
    and `_DEFAULT_ADJUSTMENT` for adjustment; they are summary nodes,
    not allocation targets, and downstream code recognises them by
    `children` being non-empty rather than by sentinel values.
    """
    rows = connection.execute(
        """
        SELECT
            pn.id,
            pn.parent_id,
            pn.category_id,
            pn.weight_micros,
            c.name AS name
        FROM plan_node pn
        JOIN category c ON c.id = pn.category_id
        WHERE pn.user_id = ?
        ORDER BY pn.parent_id, pn.id
        """,
        (user_id,),
    ).fetchall()
    by_id: dict[int, CategoryNode] = {}
    parent_of: dict[int, Optional[int]] = {}
    category_of: dict[int, int] = {}
    for row in rows:
        node = CategoryNode(
            name=str(row["name"]),
            weight=row["weight_micros"] / MICROS_SCALE,
            volatility=None,
            children=[],
        )
        node_id = int(row["id"])
        by_id[node_id] = node
        parent_of[node_id] = int(row["parent_id"]) if row["parent_id"] is not None else None
        category_of[node_id] = int(row["category_id"])
    roots: list[CategoryNode] = []
    for node_id, node in by_id.items():
        parent = parent_of[node_id]
        if parent is None:
            roots.append(node)
        else:
            by_id[parent].children.append(node)
    # Populate vol/adj on plan-leaves only. A node is a leaf iff it has
    # no plan children. The paired-NULL CHECK on `category` guarantees
    # both columns are set together, so a hit yields a complete pair; a
    # miss (NULL) is a data-integrity error and surfaces as a typed
    # exception.
    for node_id, node in by_id.items():
        if node.children:
            continue
        category_id = category_of[node_id]
        attr = get_category_attribute(connection, category_id)
        if attr is None:
            raise ValueError(
                f"Plan-leaf {node.name!r} (category_id={category_id}) has no "
                "volatility/adjustment recorded; vol/adj must be set "
                "explicitly before the plan can be loaded."
            )
        node.volatility, node.adjustment = attr
    return roots


def write_plan_tree(
    connection: sqlite3.Connection,
    user_id: int,
    nodes: Sequence[CategoryNode],
) -> None:
    """Replace the user's plan with the given tree.

    Runs in a single transaction: the prior plan is deleted (cascading
    through its `plan_node` rows) and the new tree is inserted from the
    roots down. Categories are find-or-created during the walk so a plan
    can introduce paths that the seed catalog never carried.
    """
    connection.execute("BEGIN")
    try:
        connection.execute("DELETE FROM plan_node WHERE user_id = ?", (user_id,))
        for node in nodes:
            _insert_plan_subtree(
                connection,
                user_id=user_id,
                parent_plan_id=None,
                parent_category_id=None,
                node=node,
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _insert_plan_subtree(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    parent_plan_id: Optional[int],
    parent_category_id: Optional[int],
    node: CategoryNode,
) -> None:
    """Insert one plan_node row and recurse into its children.

    `plan_node` stores tree structure and the user's parent-relative
    weight only. For plan-leaves (no children), the in-memory node's
    vol/adj are written to the merged `category` columns (migration 6),
    which are the single source of truth for intrinsic per-category
    fundamentals. Plan-leaves must carry a concrete volatility — the
    walker and CLI import enforce this — so a missing value here is a
    programmer error and raises.
    """
    category_id = find_or_create_category(connection, parent_id=parent_category_id, name=node.name)
    cursor = connection.execute(
        """
        INSERT INTO plan_node
          (user_id, parent_id, category_id, weight_micros)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            parent_plan_id,
            category_id,
            fraction_to_micros(node.weight),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to insert plan_node for {node.name!r}")
    new_plan_id = int(cursor.lastrowid)
    if not node.children:
        if node.volatility is None:
            # The YAML loader returns None for `volatility: 0.0` (a legacy
            # parser quirk in `configuration._parse_optional_volatility`),
            # so a missing in-memory volatility is tolerated iff the
            # merged `category` row already has vol/adj recorded —
            # typically because the seed loader populated it. Without
            # that fallback there is no information to use and the plan
            # is invalid; the walker (or the CLI's import-time prompt)
            # is responsible for collecting vol/adj before reaching
            # this point.
            if get_category_attribute(connection, category_id) is None:
                raise ValueError(
                    f"Plan-leaf {node.name!r} (category_id={category_id}) "
                    "has no in-memory volatility and no recorded vol/adj on "
                    "the category to fall back on; the walker must collect "
                    "explicit vol/adj before persisting a leaf."
                )
        else:
            upsert_category_attribute(
                connection,
                category_id=category_id,
                volatility=node.volatility,
                adjustment=node.adjustment,
            )
    for child in node.children:
        _insert_plan_subtree(
            connection,
            user_id=user_id,
            parent_plan_id=new_plan_id,
            parent_category_id=category_id,
            node=child,
        )


def upsert_category_attribute(
    connection: sqlite3.Connection,
    *,
    category_id: int,
    volatility: float,
    adjustment: float,
) -> None:
    """Set the intrinsic `(volatility, adjustment)` on a category.

    Both columns live directly on `category` (migration 6 merged the
    former `category_attribute` table). The paired-NULL CHECK enforces
    that they are set together. Caller passes concrete values; the seed
    loader uses this for seed-known leaves and the plan walker uses it
    whenever the user adopts a category as a plan-leaf.
    """
    connection.execute(
        """
        UPDATE category
        SET volatility_micros = ?, adjustment_micros = ?
        WHERE id = ?
        """,
        (fraction_to_micros(volatility), fraction_to_micros(adjustment), category_id),
    )


def plan_exists(connection: sqlite3.Connection, user_id: int) -> bool:
    """True when the user has at least one plan_node row."""
    row = connection.execute(
        "SELECT 1 FROM plan_node WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    return row is not None


def delete_plan(connection: sqlite3.Connection, user_id: int) -> None:
    """Remove every `plan_node` row for the user. No-op if there is no plan."""
    connection.execute("DELETE FROM plan_node WHERE user_id = ?", (user_id,))


def iter_peer_plans(
    connection: sqlite3.Connection,
    *,
    exclude_user_id: Optional[int],
) -> Iterator[tuple[str, list[CategoryNode]]]:
    """Yield `(user_name, plan_tree)` for every user that has a plan.

    Excludes `exclude_user_id` when set — used by the interactive walker
    so the user building their own plan doesn't see their own (possibly
    incomplete) tree in the suggestion list. Ordered by `user.name` so
    catalog construction is deterministic across runs.
    """
    rows = connection.execute("SELECT id, name FROM user ORDER BY name").fetchall()
    for row in rows:
        user_id = int(row["id"])
        if exclude_user_id is not None and user_id == exclude_user_id:
            continue
        tree = load_plan_tree(connection, user_id)
        if tree:
            yield str(row["name"]), tree


# ---------------------------------------------------------------------------
# Category attributes (intrinsic volatility / adjustment per category)
# ---------------------------------------------------------------------------


def get_category_attribute(
    connection: sqlite3.Connection,
    category_id: int,
) -> Optional[tuple[float, float]]:
    """Return `(volatility, adjustment)` for a category, or `None`.

    The columns live on `category` itself (migration 6) and are paired
    by a CHECK constraint — both set or both NULL. `None` means the
    category has no canonical fundamentals yet (a branch the seed
    declared, or a leaf no user has adopted). The caller surfaces it:
    the walker prompts for values, the plan loader treats it as a
    data-integrity error. There is no fallback lookup or derivation;
    every plan-leaf names its own fundamentals.
    """
    row = connection.execute(
        """
        SELECT volatility_micros, adjustment_micros
        FROM category
        WHERE id = ?
        """,
        (category_id,),
    ).fetchone()
    if row is None or row["volatility_micros"] is None:
        return None
    return row["volatility_micros"] / MICROS_SCALE, row["adjustment_micros"] / MICROS_SCALE


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def iter_mapping_paths(
    connection: sqlite3.Connection,
) -> Iterator[tuple[str, ...]]:
    """Yield every category path referenced by any mapping.

    Used by the interactive walker to surface "mapping-only" leaves —
    categories that exist in the catalog because some instrument routes
    into them, even if no user's plan has adopted them yet. Mappings are
    global (no per-user scope), so the result is the same for every user.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT cp.path
        FROM mapping m
        JOIN category_path cp ON cp.id = m.category_id
        ORDER BY cp.path
        """
    ).fetchall()
    for row in rows:
        parts = tuple(part.strip() for part in str(row["path"]).split("/") if part.strip())
        if parts:
            yield parts


def _category_is_leaf(connection: sqlite3.Connection, category_id: int) -> bool:
    """True when no other category points at `category_id` as parent.

    Leaf status is structural in the category tree (not per-plan). The
    leaf-only invariant for mappings is also enforced by triggers in the
    schema — this helper exists so the application layer can raise a
    friendly error before hitting the trigger's `RAISE(ABORT)`.
    """
    row = connection.execute(
        "SELECT 1 FROM category WHERE parent_id = ? LIMIT 1",
        (category_id,),
    ).fetchone()
    return row is None


def add_mapping(
    connection: sqlite3.Connection,
    *,
    instrument_id: int,
    category_id: int,
    weight_micros: int,
) -> None:
    """Insert one mapping row, asserting the target category is a leaf.

    The leaf check is also enforced by a schema trigger; this helper
    raises `ValueError` early so callers get a typed exception with a
    clear message rather than a generic SQLite integrity error. Sibling
    rows for the same instrument are not touched — callers are
    responsible for inserting (or deleting) every row in a single
    transaction so the per-instrument weights sum to 1.0.
    """
    if not _category_is_leaf(connection, category_id):
        raise ValueError(
            f"Mapping target category {category_id} is not a leaf; "
            "mappings can only point at leaf categories."
        )
    if weight_micros <= 0 or weight_micros > MICROS_SCALE:
        raise ValueError(
            f"Mapping weight {weight_micros} micros is outside the valid range (0, {MICROS_SCALE}]."
        )
    connection.execute(
        """
        INSERT INTO mapping (instrument_id, category_id, weight_micros)
        VALUES (?, ?, ?)
        """,
        (instrument_id, category_id, weight_micros),
    )


def resolve_category_to_plan_leaf(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    category_id: int,
) -> Optional[int]:
    """Walk up the category tree to find the deepest plan-leaf ancestor.

    Mappings target leaf categories in the global category tree, but a
    user's plan may not split a branch down that far — Tani might hold
    `Equities / EM` as a leaf while Emre splits it into
    `Equities / EM / Asia`, `EMEA`, and `Americas`. When summing
    portfolio values against Tani's plan we need to roll an EMIM mapping
    (which targets `Equities / EM / Asia`) up to `Equities / EM`.

    Algorithm: walk from `category_id` toward the root via
    `category.parent_id`. The first ancestor that is also a leaf node in
    the user's `plan_node` rows (no other plan_node has it as parent) is
    the answer. Returns `None` when no ancestor matches — the caller
    should surface that as an "uncategorised" warning, not silently
    drop the position.
    """
    row = connection.execute(
        """
        WITH RECURSIVE ancestors(id, depth) AS (
            SELECT id, 0 FROM category WHERE id = ?
            UNION ALL
            SELECT c.parent_id, a.depth + 1
            FROM ancestors a
            JOIN category c ON c.id = a.id
            WHERE c.parent_id IS NOT NULL
        )
        SELECT pn.id AS plan_node_id
        FROM ancestors a
        JOIN plan_node pn ON pn.category_id = a.id AND pn.user_id = ?
        WHERE NOT EXISTS (
            SELECT 1 FROM plan_node child
            WHERE child.user_id = ? AND child.parent_id = pn.id
        )
        ORDER BY a.depth ASC
        LIMIT 1
        """,
        (category_id, user_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return int(row["plan_node_id"])


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


def get_source_id(connection: sqlite3.Connection, adapter: str) -> int:
    """Return the surrogate `source.id` for `adapter`, or raise.

    `source` is pre-populated by migration 1 with one row per
    `KNOWN_ADAPTERS` entry. A miss here means the adapter is not a
    recognised broker — the CHECK on `source.adapter` would reject any
    fresh insert anyway, so we surface the error rather than coercing.
    """
    row = connection.execute(
        "SELECT id FROM source WHERE adapter = ?",
        (adapter,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown adapter {adapter!r}: no row in `source`")
    return int(row["id"])


def find_or_create_instrument(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    instrument_id_text: str,
    description: Optional[str],
) -> int:
    """Find or insert `(source_id, instrument_id_text)`. Updates an empty
    description with the new one but never overwrites a curated one."""
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


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------


def upsert_fx_rate(
    connection: sqlite3.Connection,
    *,
    rate_date: str,
    currency: str,
    gbp_rate: float,
) -> None:
    """Insert or update one `(date, currency)` FX rate."""
    connection.execute(
        """
        INSERT INTO fx_rate (rate_date, currency, gbp_rate_micros)
        VALUES (?, ?, ?)
        ON CONFLICT(rate_date, currency) DO UPDATE SET
            gbp_rate_micros = excluded.gbp_rate_micros
        """,
        (rate_date, currency.upper(), fraction_to_micros(gbp_rate)),
    )


def get_fx_rate(
    connection: sqlite3.Connection,
    *,
    rate_date: str,
    currency: str,
) -> Optional[float]:
    """Return the stored GBP rate for `(date, currency)`, or None."""
    row = connection.execute(
        "SELECT gbp_rate_micros FROM fx_rate WHERE rate_date = ? AND currency = ?",
        (rate_date, currency.upper()),
    ).fetchone()
    if row is None:
        return None
    return float(row["gbp_rate_micros"]) / MICROS_SCALE
