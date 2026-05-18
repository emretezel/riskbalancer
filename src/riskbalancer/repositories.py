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
from typing import Iterable, Iterator, Optional, Sequence

from .configuration import CategoryNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Integer scale used for every `*_micros` column in the schema (weights,
# volatility, adjustment, FX rates). `0.55` ⇒ `550_000`; `1.0` ⇒ `1_000_000`.
MICROS_SCALE = 1_000_000

# Integer scale used for every `*_decithou` money column. £1.2345 ⇒ `12_345`.
# Four decimal places of precision with no floating-point error.
DECITHOU_SCALE = 10_000


def fraction_to_micros(value: float) -> int:
    """Round a real-valued fraction to integer parts-per-million.

    Centralised helper for every place that writes a `*_micros` column. The
    rounding (rather than truncation) means a clean `0.62 + 0.05 + 0.13 +
    0.2` round-trips to exactly `1_000_000`.
    """
    return round(value * MICROS_SCALE)


def amount_to_decithou(value: float) -> int:
    """Round a real-valued monetary amount to integer ten-thousandths.

    Used wherever the schema expects a `*_decithou` column (positions,
    market values). Rounded so a typical float-precision input doesn't
    truncate the final penny.
    """
    return round(value * DECITHOU_SCALE)


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


def find_category_by_path(
    connection: sqlite3.Connection,
    path: str,
) -> Optional[int]:
    """Look up a category id by its full ` / `-joined path.

    Read-only: missing path components return None instead of being
    auto-created (that's `resolve_category_path`'s job). Used by the
    CRUD commands that must error cleanly when the user names a path
    that doesn't exist yet.
    """
    cleaned = " / ".join(part.strip() for part in path.split("/") if part.strip())
    if not cleaned:
        return None
    row = connection.execute(
        "SELECT id FROM category_path WHERE path = ?",
        (cleaned,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def find_instrument_by_natural_key(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    instrument_id_text: str,
) -> Optional[int]:
    """Look up an instrument id by `(source_id, instrument_id_text)`.

    Read-only counterpart to `find_or_create_instrument`. Returns None
    when the instrument has never been seen (no statement_import has
    introduced it and no `rb instrument add` has been run).
    """
    row = connection.execute(
        "SELECT id FROM instrument WHERE source_id = ? AND instrument_id_text = ?",
        (source_id, instrument_id_text.strip()),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def find_mapping_by_id(
    connection: sqlite3.Connection,
    mapping_id: int,
) -> Optional[dict]:
    """Return `{id, instrument_id, category_id, weight_micros}` for a mapping row, or None."""
    row = connection.execute(
        """
        SELECT id, instrument_id, category_id, weight_micros
        FROM mapping
        WHERE id = ?
        """,
        (mapping_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "instrument_id": int(row["instrument_id"]),
        "category_id": int(row["category_id"]),
        "weight_micros": int(row["weight_micros"]),
    }


def update_mapping(
    connection: sqlite3.Connection,
    *,
    mapping_id: int,
    category_id: Optional[int] = None,
    weight_micros: Optional[int] = None,
) -> None:
    """Update one or both editable columns on a `mapping` row.

    The schema's `mapping_target_must_be_leaf_update` trigger still
    enforces the leaf-only invariant on the new `category_id`; this
    helper does the application-side pre-check so callers get a typed
    `ValueError` with a clear message before the trigger fires.
    """
    if category_id is None and weight_micros is None:
        raise ValueError("update_mapping requires at least one of category_id or weight_micros")
    if category_id is not None and not _category_is_leaf(connection, category_id):
        raise ValueError(
            f"Mapping target category {category_id} is not a leaf; "
            "mappings can only point at leaf categories."
        )
    if weight_micros is not None and (weight_micros <= 0 or weight_micros > MICROS_SCALE):
        raise ValueError(
            f"Mapping weight {weight_micros} micros is outside the valid range (0, {MICROS_SCALE}]."
        )
    sets: list[str] = []
    params: list[object] = []
    if category_id is not None:
        sets.append("category_id = ?")
        params.append(category_id)
    if weight_micros is not None:
        sets.append("weight_micros = ?")
        params.append(weight_micros)
    params.append(mapping_id)
    connection.execute(f"UPDATE mapping SET {', '.join(sets)} WHERE id = ?", params)


def delete_mapping_by_id(
    connection: sqlite3.Connection,
    mapping_id: int,
) -> bool:
    """Remove a single mapping row by id; return True iff something was deleted."""
    cursor = connection.execute("DELETE FROM mapping WHERE id = ?", (mapping_id,))
    return cursor.rowcount > 0


def weight_sum_micros_for_instrument(
    connection: sqlite3.Connection,
    instrument_id: int,
) -> int:
    """Return the sum of `weight_micros` across every mapping for the instrument.

    The application-level invariant (§3.7) is that this sums to `1_000_000`.
    Any deviation is a warning, not an error — the schema accepts it.
    """
    row = connection.execute(
        "SELECT COALESCE(SUM(weight_micros), 0) AS total FROM mapping WHERE instrument_id = ?",
        (instrument_id,),
    ).fetchone()
    return int(row["total"])


def list_mappings(
    connection: sqlite3.Connection,
    *,
    adapter: Optional[str] = None,
    instrument_id_text: Optional[str] = None,
    category_path: Optional[str] = None,
) -> list[dict]:
    """Return mapping rows joined to instrument + source + category path.

    Filters are AND-combined. Each result row is a dict with: `id`,
    `adapter`, `instrument_id_text`, `category_path`, `weight_micros`.
    Ordered by adapter, instrument, category path so the output is
    stable across runs.
    """
    where: list[str] = []
    params: list[object] = []
    if adapter is not None:
        where.append("s.adapter = ?")
        params.append(adapter)
    if instrument_id_text is not None:
        where.append("i.instrument_id_text = ?")
        params.append(instrument_id_text.strip())
    if category_path is not None:
        cleaned = " / ".join(part.strip() for part in category_path.split("/") if part.strip())
        where.append("cp.path = ?")
        params.append(cleaned)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = connection.execute(
        f"""
        SELECT
            m.id AS id,
            s.adapter AS adapter,
            i.instrument_id_text AS instrument_id_text,
            cp.path AS category_path,
            m.weight_micros AS weight_micros
        FROM mapping m
        JOIN instrument i ON i.id = m.instrument_id
        JOIN source s ON s.id = i.source_id
        JOIN category_path cp ON cp.id = m.category_id
        {where_sql}
        ORDER BY s.adapter, i.instrument_id_text, cp.path
        """,
        params,
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "adapter": str(row["adapter"]),
            "instrument_id_text": str(row["instrument_id_text"]),
            "category_path": str(row["category_path"]),
            "weight_micros": int(row["weight_micros"]),
        }
        for row in rows
    ]


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


def latest_fx_rate_on_or_before(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    currency: str,
) -> Optional[float]:
    """Return the most recent GBP rate at or before `as_of`, or None.

    Used by the report's GBP conversion: positions store native amounts,
    and the report joins each non-GBP currency to the closest `fx_rate`
    row at or before the statement's `as_of` date.
    """
    if currency.upper() == "GBP":
        return 1.0
    row = connection.execute(
        """
        SELECT gbp_rate_micros
        FROM fx_rate
        WHERE currency = ? AND rate_date <= ?
        ORDER BY rate_date DESC
        LIMIT 1
        """,
        (currency.upper(), as_of),
    ).fetchone()
    if row is None:
        return None
    return float(row["gbp_rate_micros"]) / MICROS_SCALE


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def find_or_create_account(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    source_id: int,
    name: str,
) -> int:
    """Find or insert an `account` row for `(user_id, source_id, name)`.

    The natural key matches the schema's `UNIQUE (user_id, source_id, name)`.
    The same broker has separate rows for each user (Emre's IBKR taxable vs
    Tani's IBKR taxable are different accounts).
    """
    clean = name.strip()
    if not clean:
        raise ValueError("account name must be non-empty")
    row = connection.execute(
        "SELECT id FROM account WHERE user_id = ? AND source_id = ? AND name = ?",
        (user_id, source_id, clean),
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = connection.execute(
        "INSERT INTO account (user_id, source_id, name) VALUES (?, ?, ?)",
        (user_id, source_id, clean),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(f"Failed to insert account {clean!r}")
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Statement imports
# ---------------------------------------------------------------------------


def replace_statement_import(
    connection: sqlite3.Connection,
    *,
    account_id: int,
    as_of: str,
    statement_path: Optional[str],
) -> int:
    """Insert a fresh `statement_import` row for `(account_id, as_of)`.

    If a prior row exists for the same `(account_id, as_of)` it is deleted
    first (cascading through `position`), then a new row is inserted. This
    matches the schema doc's §3.10 contract: re-imports replace the
    previous import in a single transaction. Caller is responsible for
    wrapping in a BEGIN/COMMIT for atomicity across the position upserts.
    """
    if statement_path is not None:
        statement_path = statement_path.strip() or None
    connection.execute(
        "DELETE FROM statement_import WHERE account_id = ? AND as_of = ?",
        (account_id, as_of),
    )
    cursor = connection.execute(
        """
        INSERT INTO statement_import
          (account_id, as_of, statement_path, imported_at)
        VALUES (?, ?, ?, ?)
        """,
        (account_id, as_of, statement_path, _utc_now_iso()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError(
            f"Failed to insert statement_import (account_id={account_id}, as_of={as_of})"
        )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def insert_position(
    connection: sqlite3.Connection,
    *,
    statement_import_id: int,
    instrument_id: int,
    description: Optional[str],
    market_value_native: float,
    currency: str,
) -> None:
    """Insert one `position` row.

    Native amount, no GBP conversion. The schema's `UNIQUE
    (statement_import_id, instrument_id)` means one row per instrument per
    import — split allocations are computed at mapping-resolution time, not
    duplicated here.
    """
    if description is not None:
        description = description.strip() or None
    decithou = amount_to_decithou(market_value_native)
    if decithou < 0:
        raise ValueError("market value must be non-negative (long-only model)")
    connection.execute(
        """
        INSERT INTO position
          (statement_import_id, instrument_id, description,
           market_value_native_decithou, currency)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            statement_import_id,
            instrument_id,
            description,
            decithou,
            currency.upper(),
        ),
    )


def iter_current_positions(
    connection: sqlite3.Connection,
    *,
    user_id: int,
) -> Iterator[dict]:
    """Yield every position in the user's most-recent import per account.

    Reads through the `current_position` view. Returns dicts with the
    columns the report needs: instrument_id, instrument_id_text, adapter,
    account_name, as_of, currency, market_value_native (as a Python float
    in native units), description.
    """
    rows = connection.execute(
        """
        SELECT
            cp.id AS position_id,
            cp.instrument_id AS instrument_id,
            cp.description AS description,
            cp.market_value_native_decithou AS decithou,
            cp.currency AS currency,
            cp.as_of AS as_of,
            cp.account_id AS account_id,
            a.name AS account_name,
            s.adapter AS adapter,
            i.instrument_id_text AS instrument_id_text,
            i.description AS instrument_description
        FROM current_position cp
        JOIN account a ON a.id = cp.account_id
        JOIN source s ON s.id = a.source_id
        JOIN instrument i ON i.id = cp.instrument_id
        WHERE cp.user_id = ?
        ORDER BY s.adapter, a.name, i.instrument_id_text
        """,
        (user_id,),
    ).fetchall()
    for row in rows:
        yield {
            "position_id": int(row["position_id"]),
            "instrument_id": int(row["instrument_id"]),
            "instrument_id_text": str(row["instrument_id_text"]),
            "instrument_description": (
                str(row["instrument_description"])
                if row["instrument_description"] is not None
                else None
            ),
            "description": (str(row["description"]) if row["description"] is not None else None),
            "currency": str(row["currency"]),
            "market_value_native": float(row["decithou"]) / DECITHOU_SCALE,
            "as_of": str(row["as_of"]),
            "account_id": int(row["account_id"]),
            "account_name": str(row["account_name"]),
            "adapter": str(row["adapter"]),
        }


# ---------------------------------------------------------------------------
# Mappings (richer accessors used by CRUD commands and import)
# ---------------------------------------------------------------------------


def get_mappings_for_instrument(
    connection: sqlite3.Connection,
    instrument_id: int,
) -> list[tuple[int, int]]:
    """Return `[(category_id, weight_micros), …]` for the instrument.

    Empty list means the instrument has no mapping at all — the caller is
    expected to surface this as an "uncategorised" condition rather than
    silently dropping the position.
    """
    rows = connection.execute(
        """
        SELECT category_id, weight_micros
        FROM mapping
        WHERE instrument_id = ?
        ORDER BY id
        """,
        (instrument_id,),
    ).fetchall()
    return [(int(r["category_id"]), int(r["weight_micros"])) for r in rows]


def list_unmapped_instrument_ids(
    connection: sqlite3.Connection,
    *,
    user_id: int,
) -> list[int]:
    """Return instrument ids that the user holds but have no mapping.

    "Holds" means the instrument appears in at least one current position
    for the user. Useful for the post-import prompt that asks the user to
    categorise newly-encountered instruments.
    """
    rows = connection.execute(
        """
        SELECT DISTINCT cp.instrument_id AS id
        FROM current_position cp
        WHERE cp.user_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM mapping m WHERE m.instrument_id = cp.instrument_id
          )
        ORDER BY id
        """,
        (user_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def delete_mappings_for_instrument(
    connection: sqlite3.Connection,
    instrument_id: int,
) -> int:
    """Remove every mapping row for this instrument; return rows deleted.

    Used when the caller wants to replace an instrument's full mapping
    set with a fresh one (the per-instrument weights must sum to 1.0, so a
    partial replacement is rarely what the user wants).
    """
    cursor = connection.execute(
        "DELETE FROM mapping WHERE instrument_id = ?",
        (instrument_id,),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Bulk lookups used by the report
# ---------------------------------------------------------------------------


def get_instrument_by_id(
    connection: sqlite3.Connection,
    instrument_id: int,
) -> Optional[tuple[int, str, Optional[str]]]:
    """Return `(source_id, instrument_id_text, description)` for the row, or None."""
    row = connection.execute(
        "SELECT source_id, instrument_id_text, description FROM instrument WHERE id = ?",
        (instrument_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        int(row["source_id"]),
        str(row["instrument_id_text"]),
        str(row["description"]) if row["description"] is not None else None,
    )


def get_plan_leaf_for_node(
    connection: sqlite3.Connection,
    *,
    plan_node_id: int,
) -> dict:
    """Return summary info for a plan-leaf node id.

    Returns `{"category_id", "path", "volatility", "adjustment"}` — the
    fields the report needs to aggregate by leaf and compute risk-parity
    weights. Raises if the node has children (i.e. isn't a leaf) or if
    fundamentals are missing on the leaf's category.
    """
    row = connection.execute(
        """
        SELECT pn.category_id AS category_id,
               cp.path AS path,
               c.volatility_micros AS vol_micros,
               c.adjustment_micros AS adj_micros
        FROM plan_node pn
        JOIN category c ON c.id = pn.category_id
        JOIN category_path cp ON cp.id = pn.category_id
        WHERE pn.id = ?
        """,
        (plan_node_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"plan_node {plan_node_id} not found")
    if row["vol_micros"] is None:
        raise ValueError(
            f"plan-leaf at {row['path']!r} is missing volatility/adjustment fundamentals"
        )
    return {
        "category_id": int(row["category_id"]),
        "path": str(row["path"]),
        "volatility": float(row["vol_micros"]) / MICROS_SCALE,
        "adjustment": float(row["adj_micros"]) / MICROS_SCALE,
    }


def currencies_in_current_positions(
    connection: sqlite3.Connection,
    *,
    user_id: int,
) -> Iterable[str]:
    """Return the distinct set of currencies the user currently holds."""
    rows = connection.execute(
        """
        SELECT DISTINCT currency
        FROM current_position
        WHERE user_id = ?
        ORDER BY currency
        """,
        (user_id,),
    ).fetchall()
    return [str(row["currency"]) for row in rows]
