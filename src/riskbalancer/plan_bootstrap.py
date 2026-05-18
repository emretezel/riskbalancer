"""
Plan bootstrap: build a new user's category plan by walking the global
category tree stored in the DB.

The catalog the walker offers is unioned from three DB sources, in priority
order: peer-user plans, categories with explicit fundamentals on the
`category` table, and category paths referenced by any mapping. The
interactive walker then lets the user pick, at every level of the tree,
which categories to keep and how to weight them.

Author: Emre Tezel
"""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence

from . import repositories
from .configuration import (
    CategoryNode,
    collect_category_weight_validation_failures,
    format_category_weight_validation_failures,
)
from .repositories import MICROS_SCALE

DEFAULT_ADJUSTMENT = 1.0

# Sentinel used at every pick prompt so the user can introduce categories that
# don't yet exist in the catalog. Accepted in either form (`+ new` or `new`).
NEW_CATEGORY_SENTINEL = "+ new"
_NEW_CATEGORY_KEYWORDS = {NEW_CATEGORY_SENTINEL, "new"}
_EXIT_KEYWORDS = {"quit", "exit"}


class PlanCreationAborted(Exception):
    """Raised when the user asks to abandon the interactive walker.

    Triggered by typing `quit` or `exit` at any prompt, by Ctrl+C
    (KeyboardInterrupt), by EOF on stdin, or by declining the final save
    confirmation. The CLI catches this and exits cleanly without writing
    anything to disk.
    """


@dataclass
class CatalogNode:
    """A single option in the bootstrap catalog.

    Unlike `CategoryNode`, the catalog is descriptive (a menu of choices) not
    prescriptive (a plan that must validate). `suggested_weight`,
    `suggested_volatility`, and `suggested_adjustment` are informational hints
    drawn from whichever source the node was first seen in. `from_mappings`
    flags leaves that exist only because a mapping file references them.

    Whether a picked node ends up a branch or a leaf in the user's plan is
    decided per-pick at walk time (see `_prompt_branch_or_leaf`); it is not
    stored on the catalog node itself.
    """

    name: str
    suggested_weight: Optional[float] = None
    suggested_volatility: Optional[float] = None
    suggested_adjustment: Optional[float] = None
    children: list["CatalogNode"] = field(default_factory=list)
    from_mappings: bool = False


# ---------------------------------------------------------------------------
# Catalog construction
# ---------------------------------------------------------------------------


def build_catalog_from_db(
    connection: sqlite3.Connection,
    *,
    current_user_id: Optional[int],
) -> list[CatalogNode]:
    """Build the merged catalog by querying the database.

    Priority order, identical in spirit to the YAML version:

    1. Peer-user plans (deterministic by `user.name`). First peer that has
       a given category wins on `suggested_weight`, `suggested_volatility`,
       and `suggested_adjustment`.
    2. Seed defaults from `category` (the merged vol/adj columns from
       migration 6). Fills in volatility / adjustment suggestions for
       leaves the seed defined but no peer plan has adopted yet.
    3. Shared mapping leaves. Categories referenced by mappings but absent
       from every peer plan are inserted with `from_mappings=True` so the
       walker can flag them.

    Categories that exist in the DB only because a previous seed loaded
    them and no plan or mapping references them are intentionally
    invisible — the user does not need to see structure with no signal
    behind it.
    """
    catalog: list[CatalogNode] = []
    for _peer_name, peer_tree in repositories.iter_peer_plans(
        connection, exclude_user_id=current_user_id
    ):
        _merge_nodes_into_catalog(peer_tree, catalog)
    _merge_seed_leaves_into_catalog(connection, catalog)
    for leaf_path in repositories.iter_mapping_paths(connection):
        _ensure_leaf_in_catalog(catalog, leaf_path)
    return catalog


def _merge_seed_leaves_into_catalog(
    connection: sqlite3.Connection,
    catalog: list[CatalogNode],
) -> None:
    """Fill in volatility / adjustment suggestions from `category`.

    Every `category` row with non-NULL vol/adj corresponds to a category
    whose intrinsic fundamentals are known — typically a seed leaf, but
    also any category a user has already adopted as a plan-leaf. Walking
    the row's full path ensures the catalog has the ancestor chain and
    the leaf node's suggestions are filled in (without overwriting an
    earlier peer-derived value, since gap-fill is the convention
    everywhere else in this module). Branches whose vol/adj is unset
    have NULL in these columns; their branch-level suggestions come
    from peer plans if at all.
    """
    rows = connection.execute(
        """
        SELECT c.volatility_micros, c.adjustment_micros, cp.path
        FROM category c
        JOIN category_path cp ON cp.id = c.id
        WHERE c.volatility_micros IS NOT NULL
        ORDER BY cp.path
        """
    ).fetchall()
    for row in rows:
        path = tuple(part.strip() for part in str(row["path"]).split("/") if part.strip())
        if not path:
            continue
        vol = row["volatility_micros"] / MICROS_SCALE
        adj = row["adjustment_micros"] / MICROS_SCALE
        cursor = catalog
        for index, segment in enumerate(path):
            is_leaf = index == len(path) - 1
            existing = _find_by_name(cursor, segment)
            if existing is None:
                existing = CatalogNode(
                    name=segment,
                    suggested_volatility=vol if is_leaf else None,
                    suggested_adjustment=adj if is_leaf else None,
                )
                cursor.append(existing)
            elif is_leaf:
                if existing.suggested_volatility is None:
                    existing.suggested_volatility = vol
                if existing.suggested_adjustment is None:
                    existing.suggested_adjustment = adj
            cursor = existing.children


def _merge_nodes_into_catalog(source: Sequence[CategoryNode], catalog: list[CatalogNode]) -> None:
    for node in source:
        existing = _find_by_name(catalog, node.name)
        if existing is None:
            catalog.append(_catalog_node_from_category(node))
        else:
            # Higher-priority sources are merged first; only fill gaps for the
            # already-known node so peer wins over seed on suggestion fields.
            if existing.suggested_volatility is None and node.volatility is not None:
                existing.suggested_volatility = node.volatility
            if existing.suggested_adjustment is None:
                existing.suggested_adjustment = node.adjustment
            if existing.suggested_weight is None:
                existing.suggested_weight = node.weight
            _merge_nodes_into_catalog(node.children, existing.children)


def _catalog_node_from_category(node: CategoryNode) -> CatalogNode:
    return CatalogNode(
        name=node.name,
        suggested_weight=node.weight,
        suggested_volatility=node.volatility,
        suggested_adjustment=node.adjustment,
        children=[_catalog_node_from_category(child) for child in node.children],
    )


def _find_by_name(catalog: Sequence[CatalogNode], name: str) -> Optional[CatalogNode]:
    for node in catalog:
        if node.name == name:
            return node
    return None


def _ensure_leaf_in_catalog(catalog: list[CatalogNode], path: tuple[str, ...]) -> None:
    cursor = catalog
    for index, segment in enumerate(path):
        existing = _find_by_name(cursor, segment)
        if existing is None:
            existing = CatalogNode(name=segment, from_mappings=True)
            cursor.append(existing)
        elif index == len(path) - 1 and not existing.children:
            # Reached the leaf in an existing tree — keep its metadata.
            pass
        cursor = existing.children


# ---------------------------------------------------------------------------
# Interactive walk
# ---------------------------------------------------------------------------


class IO(Protocol):
    """Tiny IO seam so the walker is testable without real stdin."""

    def prompt(self, message: str) -> str: ...
    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...


class StdIO:
    """Default IO that talks to the real terminal."""

    def prompt(self, message: str) -> str:
        return input(message).strip()

    def info(self, message: str) -> None:
        print(message)

    def warn(self, message: str) -> None:
        print(message, file=sys.stderr)


@dataclass
class ScriptedIO:
    """Test IO that returns a scripted sequence of answers and records output."""

    answers: list[str]
    info_log: list[str] = field(default_factory=list)
    warn_log: list[str] = field(default_factory=list)
    _index: int = 0

    def prompt(self, message: str) -> str:
        if self._index >= len(self.answers):
            raise AssertionError(f"ScriptedIO exhausted at prompt: {message!r}")
        answer = self.answers[self._index]
        self._index += 1
        return answer

    def info(self, message: str) -> None:
        self.info_log.append(message)

    def warn(self, message: str) -> None:
        self.warn_log.append(message)


def _ask(io: IO, message: str) -> str:
    """Wrap `io.prompt` so every walker prompt can be aborted uniformly.

    Raises `PlanCreationAborted` when the user types `quit` / `exit`
    (case-insensitive, leading/trailing whitespace ignored), presses Ctrl+C,
    or sends EOF. Otherwise returns the raw answer untouched so each caller
    can apply its own parsing (`.strip()`, `.lower()`, regex, etc.) just as
    it did when prompts went directly through `io.prompt`.
    """
    try:
        raw = io.prompt(message)
    except (KeyboardInterrupt, EOFError) as exc:
        raise PlanCreationAborted("interrupted by user") from exc
    if raw.strip().lower() in _EXIT_KEYWORDS:
        raise PlanCreationAborted("user requested exit")
    return raw


def _prompt_yes_no(io: IO, message: str, *, default: bool) -> bool:
    """Tiny y/N prompt that routes through `_ask` so quit/Ctrl+C still abort.

    Empty input returns `default`. Anything other than y/yes/n/no/empty
    re-prompts with a warning.
    """
    while True:
        raw = _ask(io, message).strip().lower()
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        if raw == "":
            return default
        io.warn("Please answer y or n.")


def walk_catalog_interactive(
    catalog: Sequence[CatalogNode],
    io: IO,
) -> list[CategoryNode]:
    """Run the recursive pick-one/weight/add-another loop and return a plan."""
    if not catalog:
        raise ValueError(
            "Catalog is empty: no plan files or mappings are visible. "
            "Add at least one plan or seed_plan.yaml before running plan create."
        )
    io.info("Type 'quit' or 'exit' (or press Ctrl+C) at any prompt to abort without saving.")
    return _walk_level(
        list(catalog),
        io,
        level_label="top level",
        path_prefix=(),
    )


def _walk_level(
    options: list[CatalogNode],
    io: IO,
    *,
    level_label: str,
    path_prefix: tuple[str, ...],
    inherited_volatility: Optional[float] = None,
    inherited_adjustment: Optional[float] = None,
) -> list[CategoryNode]:
    io.info(f"\n—— {level_label} ——")
    # Each pick carries its name, weight, and the per-pick branch/leaf decision
    # captured at decision time via `_prompt_branch_or_leaf`. The bool replaces
    # the older "infer from catalog children" heuristic so a user can flatten
    # a catalog branch or promote a catalog leaf without leaving the walker.
    picked: list[tuple[CatalogNode, float, bool]] = []
    # Loop without an early break on empty `remaining`: the `+ new` sentinel
    # is always offered, so a level with no catalog options (e.g. children of
    # a user-added synthetic branch, or of a promoted catalog leaf) is still
    # reachable. The user controls when the loop ends via the "add another?"
    # prompt.
    while True:
        picked_ids = {id(node) for node, _, _ in picked}
        remaining = [node for node in options if id(node) not in picked_ids]
        chosen = _prompt_pick_one(io, remaining, level_label, picked)
        treat_as_branch = _prompt_branch_or_leaf(io, chosen)
        weight = _prompt_weight(io, chosen, level_label)
        picked.append((chosen, weight, treat_as_branch))
        if not _prompt_add_another(io, level_label, len(picked) == 1):
            break
    if not picked:
        raise ValueError(f"At least one asset class must be added at {level_label}")
    _validate_level_weights(picked, io, level_label)

    plan_nodes: list[CategoryNode] = []
    for catalog_node, weight, treat_as_branch in picked:
        node_path = path_prefix + (catalog_node.name,)
        node_label = " / ".join(node_path)
        next_inherited_vol = catalog_node.suggested_volatility or inherited_volatility
        next_inherited_adj = catalog_node.suggested_adjustment or inherited_adjustment
        if treat_as_branch:
            child_plan = _walk_level(
                list(catalog_node.children),
                io,
                level_label=node_label,
                path_prefix=node_path,
                inherited_volatility=next_inherited_vol,
                inherited_adjustment=next_inherited_adj,
            )
            plan_nodes.append(
                CategoryNode(
                    name=catalog_node.name,
                    weight=weight,
                    volatility=catalog_node.suggested_volatility,
                    adjustment=catalog_node.suggested_adjustment or DEFAULT_ADJUSTMENT,
                    children=child_plan,
                )
            )
        else:
            volatility, adjustment = _prompt_leaf_metadata(
                io,
                node_label,
                catalog_node,
                inherited_volatility=next_inherited_vol,
                inherited_adjustment=next_inherited_adj,
            )
            plan_nodes.append(
                CategoryNode(
                    name=catalog_node.name,
                    weight=weight,
                    volatility=volatility,
                    adjustment=adjustment,
                    children=[],
                )
            )
    return plan_nodes


def _prompt_branch_or_leaf(io: IO, chosen: CatalogNode) -> bool:
    """Ask whether `chosen` is a branch (has sub-categories) or a leaf.

    Default mirrors the catalog so the common case is press-Enter: a node
    with catalog children defaults to Y (recurse, preserving the catalog
    structure); a catalog leaf or a freshly added `+ new` node defaults to N
    (treat as a leaf, prompt for vol/adj). Routes through `_prompt_yes_no`
    so quit / exit / Ctrl+C abort cleanly like every other walker prompt.
    """
    has_catalog_children = bool(chosen.children)
    suffix = "[Y/n]" if has_catalog_children else "[y/N]"
    return _prompt_yes_no(
        io,
        f"Does {chosen.name} have sub-categories? {suffix}: ",
        default=has_catalog_children,
    )


def _prompt_pick_one(
    io: IO,
    remaining: list[CatalogNode],
    level_label: str,
    picked: list[tuple[CatalogNode, float, bool]],
) -> CatalogNode:
    """Prompt the user to pick a category at the current level.

    The displayed options are the remaining catalog nodes plus the sentinel
    `+ new` so the user can always introduce a category that doesn't yet
    exist. Catalog matches take precedence over the sentinel: if a catalog
    category happens to be named "new", typing `new` will pick it; the
    sentinel is reachable as `+ new` in that case.
    """
    labels = [_decorate_label(node) for node in remaining]
    labels.append(NEW_CATEGORY_SENTINEL)
    name_to_node = {node.name.lower(): node for node in remaining}
    # Sibling names guard the "+ new" sub-flow so a synthetic node can't
    # collide with another sibling — already-picked or still-available.
    sibling_names = {node.name.lower() for node, _, _ in picked} | set(name_to_node.keys())
    progress = (
        ", ".join(f"{node.name}={int(round(weight * 100))}%" for node, weight, _ in picked)
        if picked
        else "none yet"
    )
    while True:
        raw = _ask(
            io,
            f"Select an asset class to add to {level_label} "
            f"[{', '.join(labels)}] (assigned so far: {progress}): ",
        )
        cleaned = raw.strip().lower()
        if cleaned in name_to_node:
            return name_to_node[cleaned]
        if cleaned in _NEW_CATEGORY_KEYWORDS:
            return _prompt_new_category(io, level_label, sibling_names)
        io.warn(f"Unknown asset class '{raw.strip()}'. Choose one of the listed options.")


def _prompt_new_category(
    io: IO,
    level_label: str,
    sibling_names: set[str],
) -> CatalogNode:
    """Collect a brand-new category name from the user and return a synthetic node.

    Asks for a name (rejecting empty input, the reserved `new` / `+ new`
    keywords, and collisions with sibling names already at this level). The
    branch-vs-leaf decision is asked uniformly afterwards by
    `_prompt_branch_or_leaf` in `_walk_level`, so every pick (catalog or
    synthetic) goes through the same single question in the same place.
    """
    while True:
        raw = _ask(io, f"Name for new category at {level_label}: ").strip()
        if not raw:
            io.warn("Name cannot be empty.")
            continue
        if raw.lower() in _NEW_CATEGORY_KEYWORDS:
            io.warn("'new' / '+ new' is a reserved keyword — pick another name.")
            continue
        if raw.lower() in sibling_names:
            io.warn(f"'{raw}' already exists at this level.")
            continue
        break
    return CatalogNode(name=raw)


def _decorate_label(node: CatalogNode) -> str:
    if node.from_mappings:
        return f"{node.name} (from mappings)"
    return node.name


def _prompt_weight(io: IO, chosen: CatalogNode, level_label: str) -> float:
    suggestion = _format_weight_suggestion(chosen.suggested_weight)
    while True:
        raw = _ask(io, f"Risk weight for {chosen.name} at {level_label}{suggestion}: ")
        try:
            return _parse_weight_input(raw)
        except ValueError as exc:
            io.warn(str(exc))


def _format_weight_suggestion(weight: Optional[float]) -> str:
    """Render a `(catalog suggests N%)` clause for the weight prompt.

    A naive `int(round(weight*100))%` displays a small non-zero suggestion
    (e.g. 0.4%) as "0%", which then misleads the user about what numeric
    value is acceptable. Fall back to two decimals when the rounded form
    would lose a non-zero value.
    """
    if weight is None:
        return ""
    pct = weight * 100
    if 0 < pct < 0.5:
        return f" (catalog suggests {pct:.2f}%)"
    return f" (catalog suggests {int(round(pct))}%)"


def _prompt_add_another(io: IO, level_label: str, only_one_so_far: bool) -> bool:
    return _prompt_yes_no(io, f"Add another asset class to {level_label}? [y/N]: ", default=False)


def _validate_level_weights(
    picked: list[tuple[CatalogNode, float, bool]], io: IO, level_label: str
) -> None:
    """Ensure the entered weights at this level sum to 100%; re-prompt on failure.

    Builds an artificial CategoryNode list and runs the existing validator so
    the same tolerance applies as for the final plan check. The per-pick
    branch/leaf bool is preserved across re-prompts so the user does not get
    asked the sub-categories question again.
    """
    while True:
        artificial = [
            CategoryNode(name=node.name, weight=weight, volatility=0.1)
            for node, weight, _ in picked
        ]
        failures = collect_category_weight_validation_failures(artificial)
        if not failures:
            io.info(f"✓ {level_label} sums to 100%")
            return
        io.warn(format_category_weight_validation_failures(failures))
        io.info(f"Re-enter the weights for {level_label}:")
        for index, (node, _weight, treat_as_branch) in enumerate(picked):
            new_weight = _prompt_weight(io, node, level_label)
            picked[index] = (node, new_weight, treat_as_branch)


def _prompt_leaf_metadata(
    io: IO,
    node_label: str,
    catalog_node: CatalogNode,
    *,
    inherited_volatility: Optional[float],
    inherited_adjustment: Optional[float],
) -> tuple[float, float]:
    """Ask the user for a leaf's volatility and adjustment.

    Defaults follow a strict "catalog suggestion or inherited" chain. If
    neither source has a value (e.g. the user flattened a catalog branch
    that had no vol/adj of its own, or added a `+ new` leaf at top level),
    the prompt offers no default and forces explicit input — the walker
    refuses to invent a number when it has nothing to suggest, per
    CLAUDE.md's "no magic values" rule.
    """
    suggested_vol = (
        catalog_node.suggested_volatility
        if catalog_node.suggested_volatility is not None
        else inherited_volatility
    )
    suggested_adj = (
        catalog_node.suggested_adjustment
        if catalog_node.suggested_adjustment is not None
        else inherited_adjustment
    )
    volatility = _prompt_positive_float(
        io,
        f"Volatility for {node_label} {_format_metadata_hint(suggested_vol)}: ",
        default=suggested_vol,
    )
    adjustment = _prompt_positive_float(
        io,
        f"Adjustment for {node_label} {_format_metadata_hint(suggested_adj)}: ",
        default=suggested_adj,
    )
    return volatility, adjustment


def _format_metadata_hint(suggestion: Optional[float]) -> str:
    """Render the bracketed hint shown next to a leaf vol/adj prompt.

    A concrete suggestion preserves the existing "[catalog suggests X]"
    label so press-Enter still works. With no suggestion, the prompt makes
    the no-default state explicit so the user knows blank input will be
    rejected.
    """
    if suggestion is None:
        return "[no default; please enter a value]"
    return f"[catalog suggests {suggestion}]"


def _prompt_positive_float(io: IO, message: str, *, default: Optional[float]) -> float:
    """Read a positive float, optionally allowing blank-means-default.

    A `default=None` signals that the caller has nothing to suggest, so
    blank input is rejected and the user must type a value. A non-None
    default keeps the existing press-Enter-to-accept behaviour.
    """
    while True:
        raw = _ask(io, message).strip()
        if not raw:
            if default is None:
                io.warn("This field has no default — please enter a positive number.")
                continue
            return default
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError("value must be positive")
            return value
        except ValueError:
            io.warn("Enter a positive number.")


def fill_missing_leaf_vol_adj(
    connection: sqlite3.Connection,
    nodes: Sequence[CategoryNode],
    io: IO,
) -> None:
    """Ensure every plan-leaf has a concrete volatility/adjustment in memory.

    Walks `nodes` depth-first. For each leaf whose `volatility is None`:

    1. Looks up the category by path in the DB (without creating any
       rows). If the row already has `volatility_micros` /
       `adjustment_micros` set, copies them onto the in-memory node —
       previously-defined fundamentals are reused silently.
    2. Otherwise, prompts the user via the same `_prompt_leaf_metadata`
       used by the interactive walker, defaulting to the closest
       ancestor's vol/adj if any. Raises `PlanCreationAborted` if the
       prompt cancels.

    Mutates `nodes` in place. The interactive walker already collects
    vol/adj at leaf time, so this helper is only meaningful for the CSV
    import path where blank cells produce leaves with `volatility=None`.
    """
    _fill_missing_leaf_vol_adj(
        connection,
        nodes,
        io,
        path_prefix=(),
        inherited_volatility=None,
        inherited_adjustment=None,
    )


def _fill_missing_leaf_vol_adj(
    connection: sqlite3.Connection,
    nodes: Sequence[CategoryNode],
    io: IO,
    *,
    path_prefix: tuple[str, ...],
    inherited_volatility: Optional[float],
    inherited_adjustment: Optional[float],
) -> None:
    """Recursive implementation of `fill_missing_leaf_vol_adj`."""
    for node in nodes:
        path = path_prefix + (node.name,)
        # Carry the node's own vol/adj down to descendants if set; this
        # matches the walker's "inherited from nearest ancestor" rule.
        next_inherited_vol = (
            node.volatility if node.volatility is not None else inherited_volatility
        )
        next_inherited_adj = (
            node.adjustment if node.adjustment not in (None, 1.0) else inherited_adjustment
        )
        if node.children:
            _fill_missing_leaf_vol_adj(
                connection,
                node.children,
                io,
                path_prefix=path,
                inherited_volatility=next_inherited_vol,
                inherited_adjustment=next_inherited_adj,
            )
            continue
        # Leaf branch.
        if node.volatility is not None:
            continue
        # In-memory vol is missing. Try the DB first — a previous plan
        # or seed load may have recorded fundamentals for this category.
        category_id = _find_category_id_by_path(connection, path)
        if category_id is not None:
            attrs = repositories.get_category_attribute(connection, category_id)
            if attrs is not None:
                node.volatility, node.adjustment = attrs
                continue
        # Nothing in the DB either. Prompt the user. Treat the node's
        # CSV-provided adjustment as the suggestion if it differs from
        # the silent 1.0 default; otherwise fall back to inherited.
        suggested_adj = node.adjustment if node.adjustment not in (None, 1.0) else None
        synthetic = CatalogNode(
            name=node.name,
            suggested_volatility=None,
            suggested_adjustment=suggested_adj,
        )
        label = " / ".join(path)
        volatility, adjustment = _prompt_leaf_metadata(
            io,
            label,
            synthetic,
            inherited_volatility=inherited_volatility,
            inherited_adjustment=inherited_adjustment,
        )
        node.volatility = volatility
        node.adjustment = adjustment


def _find_category_id_by_path(
    connection: sqlite3.Connection,
    path: tuple[str, ...],
) -> Optional[int]:
    """Resolve a category path to its id without creating any rows.

    Mirrors `seed._find_or_create_category`'s lookup logic but returns
    `None` for any missing path component instead of inserting. Used by
    `fill_missing_leaf_vol_adj` so the read-side pre-check has no DB
    side effects — the actual category rows are created later by
    `repositories.write_plan_tree` inside its transaction.
    """
    parent_id: Optional[int] = None
    for name in path:
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
        if row is None:
            return None
        parent_id = int(row["id"])
    return parent_id


_WEIGHT_RE = re.compile(r"^\s*([0-9.]+)\s*%?\s*$")


def _parse_weight_input(raw: str) -> float:
    """Parse a weight as either a percentage (e.g. 55) or fraction (e.g. 0.55).

    A bare `0` is accepted and means "this category contributes 0% at this
    level" — useful when a user wants to keep a category in the structure
    (e.g. for tracking sub-categories or future re-allocation) but holds no
    weight today. The level-sum-to-100% validator still applies, so the
    other siblings must absorb the missing weight.
    """
    match = _WEIGHT_RE.match(raw)
    if not match:
        raise ValueError(
            f"Invalid weight '{raw}'. Use a percentage (e.g. 55) or fraction (e.g. 0.55)."
        )
    value = float(match.group(1))
    if value > 1:
        value /= 100.0
    if value < 0:
        raise ValueError("Weights must not be negative")
    if value > 1.0 + 1e-9:
        raise ValueError("Weights must not exceed 100%")
    return value


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_plan_tree(nodes: Sequence[CategoryNode]) -> str:
    """Render a plan tree as an indented multi-line summary for confirmation.

    Each line is `<indent><name>` padded to a column, followed by the weight
    as a percentage. Leaves additionally show `vol=<v>  adj=<a>` so the user
    can verify the values they entered before the plan lands on disk.
    """
    out: list[str] = []
    _append_tree_lines(nodes, indent=0, out=out)
    return "\n".join(out)


def _append_tree_lines(nodes: Sequence[CategoryNode], *, indent: int, out: list[str]) -> None:
    for node in nodes:
        prefix = "  " * indent
        weight_pct = f"{int(round(node.weight * 100))}%"
        name_field = f"{prefix}{node.name}"
        if node.children:
            out.append(f"{name_field:<40} {weight_pct:>5}")
            _append_tree_lines(node.children, indent=indent + 1, out=out)
        else:
            vol_text = f"{node.volatility}" if node.volatility is not None else "—"
            details = f"vol={vol_text}  adj={node.adjustment}"
            out.append(f"{name_field:<40} {weight_pct:>5}   {details}")


# ---------------------------------------------------------------------------
# Catalog helpers used by the CLI
# ---------------------------------------------------------------------------


def count_unique_categories(catalog: Sequence[CatalogNode]) -> int:
    """Total node count across the entire catalog tree (for telemetry only)."""
    total = 0
    for node in catalog:
        total += 1
        total += count_unique_categories(node.children)
    return total


def describe_catalog_sources_from_db(
    connection: sqlite3.Connection,
    *,
    current_user_name: str,
) -> str:
    """Render a one-line summary of where the DB-backed catalog drew from.

    Mirrors `describe_catalog_sources` but reads from the database instead
    of the filesystem. Used by `plan create` to show the user which peers
    contributed before the interactive walk begins.
    """
    parts: List[str] = []
    peer_rows = connection.execute(
        """
        SELECT u.name
        FROM user u
        WHERE u.name != ?
          AND EXISTS (SELECT 1 FROM plan_node pn WHERE pn.user_id = u.id)
        ORDER BY u.name
        """,
        (current_user_name,),
    ).fetchall()
    if peer_rows:
        names = [row["name"] for row in peer_rows]
        parts.append(f"{len(names)} peer plan(s): {', '.join(names)}")
    seed_count = connection.execute(
        "SELECT COUNT(*) AS n FROM category WHERE volatility_micros IS NOT NULL"
    ).fetchone()["n"]
    if seed_count:
        parts.append(f"{seed_count} category default(s)")
    mapping_count = connection.execute("SELECT COUNT(*) AS n FROM mapping").fetchone()["n"]
    if mapping_count:
        parts.append(f"{mapping_count} mapping row(s)")
    if not parts:
        return "Catalog: empty"
    return "Catalog: " + ", ".join(parts)
