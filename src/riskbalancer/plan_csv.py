"""
CSV import/export for a user's category plan.

The on-disk source of truth is `plan.yaml` (nested categories with weights,
volatility, and adjustment). This module mirrors that tree in a flat,
spreadsheet-friendly **leaves-only** CSV: one row per leaf, with each
level's name and per-level weight interleaved alongside each other so a
single row carries the full path from root to leaf and the weight at every
depth along that path.

Header shape (where N is the maximum leaf depth):

    level1,weight1,level2,weight2,...,levelN,weightN,volatility,adjustment

Round-trip discipline: per-level weights for an intermediate node will
appear on every sibling leaf under it, and the reader checks they agree.
A disagreement raises `PlanCSVError` so a hand-edit that breaks
consistency cannot silently produce a different plan than the user
expected. Branch-level volatility is resolved into the leaf's
`volatility` column on export and reconstructed branches carry no
volatility on import — the loader's leaf-volatility inheritance means
this has no observable effect for plans like the seed.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence, TextIO

# These parse helpers are defined as private in `configuration` because they
# are implementation details of the YAML loader. We import them directly so
# CSV import accepts the same inputs as YAML — percent suffixes on weights,
# blank-means-default for volatility/adjustment, and identical bounds checks
# — without duplicating the parse logic. The same cross-module-private
# pattern is used by `plan_adjust` (it imports `_ask` / `_prompt_yes_no`
# from `plan_bootstrap`).
from .configuration import (
    CategoryNode,
    _parse_adjustment,
    _parse_optional_volatility,
    _parse_weight,
)

# Trailing column names (in canonical order). The variable-width prefix is
# `level1, weight1, level2, weight2, ...` — see `_build_header`.
WEIGHT_COLUMN = "weight"
VOLATILITY_COLUMN = "volatility"
ADJUSTMENT_COLUMN = "adjustment"
TRAILING_COLUMNS: tuple[str, ...] = (VOLATILITY_COLUMN, ADJUSTMENT_COLUMN)
LEVEL_COLUMN_PREFIX = "level"
WEIGHT_COLUMN_PREFIX = "weight"

# Two cells per depth in the variable-width prefix (level + weight).
CELLS_PER_LEVEL = 2

# Tolerance for treating two repeated per-level weights as "the same".
# Repeated weights are written via `repr` so they round-trip byte-equal,
# but a user editing the spreadsheet might enter `0.75` and `0.7500000001`
# and reasonably expect them to be treated as identical.
WEIGHT_CONFLICT_TOLERANCE = 1e-9


class PlanCSVError(ValueError):
    """Raised when a plan CSV cannot be parsed or is structurally invalid.

    `row_number` is 1-indexed and counts the header as row 1, matching the
    gutter numbering a spreadsheet user sees, so error messages are easy to
    locate. Subclasses `ValueError` so callers can catch either the typed
    exception (for the row context) or `ValueError` (for "any parse failure").
    """

    def __init__(self, message: str, *, row_number: Optional[int] = None) -> None:
        if row_number is not None:
            super().__init__(f"row {row_number}: {message}")
        else:
            super().__init__(message)
        self.row_number = row_number


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def write_plan_csv(nodes: Sequence[CategoryNode], stream: TextIO) -> None:
    """Render `nodes` as a leaves-only interleaved CSV onto `stream`.

    Header is `level1,weight1,...,levelN,weightN,volatility,adjustment`
    where N is the maximum leaf depth. Each row represents one leaf,
    written in DFS pre-order so leaves appear in their plan-order
    sequence. The leaf row carries the full path (level cells), the
    per-level weight at each depth (weight cells), the leaf's resolved
    volatility (own value, falling back to the nearest ancestor that
    defined one), and the leaf's own adjustment.

    Float formatting uses `repr` so an unedited round-trip is byte-stable.
    """
    writer = csv.writer(stream)
    max_depth = _max_depth(nodes)
    if max_depth == 0:
        # Empty plan — emit just the trailing-column header. The reader will
        # reject an empty body via `build_portfolio_plan_from_nodes` (root
        # totals 0%); we still write a parseable file so the export does not
        # silently produce a blank file.
        writer.writerow(list(TRAILING_COLUMNS))
        return
    writer.writerow(_build_header(max_depth))
    for path, weight_chain, volatility, adjustment in _iter_leaves_with_chain(nodes):
        row: list[str] = []
        for level_index in range(max_depth):
            if level_index < len(path):
                row.append(path[level_index])
                row.append(_format_number(weight_chain[level_index]))
            else:
                # Leaf is shallower than the deepest leaf in the tree —
                # trailing level/weight cells stay blank.
                row.append("")
                row.append("")
        row.append("" if volatility is None else _format_number(volatility))
        row.append(_format_number(adjustment))
        writer.writerow(row)


def _build_header(max_depth: int) -> list[str]:
    """Build the interleaved header row for a tree of depth `max_depth`."""
    header: list[str] = []
    for depth in range(1, max_depth + 1):
        header.append(f"{LEVEL_COLUMN_PREFIX}{depth}")
        header.append(f"{WEIGHT_COLUMN_PREFIX}{depth}")
    header.extend(TRAILING_COLUMNS)
    return header


def _max_depth(nodes: Sequence[CategoryNode]) -> int:
    """Return the deepest path length in the tree (0 for an empty list)."""
    deepest = 0
    for node in nodes:
        deepest = max(deepest, 1 + _max_depth(node.children))
    return deepest


def _iter_leaves_with_chain(
    nodes: Sequence[CategoryNode],
    *,
    path_prefix: tuple[str, ...] = (),
    weight_prefix: tuple[float, ...] = (),
    inherited_volatility: Optional[float] = None,
) -> Iterator[tuple[tuple[str, ...], tuple[float, ...], Optional[float], float]]:
    """Yield `(path, per_level_weights, resolved_volatility, adjustment)` for every leaf.

    `per_level_weights[i]` is the weight of the ancestor at depth `i+1`
    (i.e., the row's `weighti` cell). `resolved_volatility` is the leaf's
    own volatility if set, else the nearest ancestor's volatility, else
    None — this matches the loader's inheritance chain so the leaves-only
    CSV is self-sufficient.
    """
    for node in nodes:
        node_path = path_prefix + (node.name,)
        node_weights = weight_prefix + (node.weight,)
        # The loader treats a non-positive volatility as "no value", so
        # only propagate it when it is actually informative.
        next_inherited = node.volatility if node.volatility is not None else inherited_volatility
        if node.children:
            yield from _iter_leaves_with_chain(
                node.children,
                path_prefix=node_path,
                weight_prefix=node_weights,
                inherited_volatility=next_inherited,
            )
        else:
            resolved_volatility = (
                node.volatility if node.volatility is not None else (inherited_volatility)
            )
            yield node_path, node_weights, resolved_volatility, node.adjustment


def _format_number(value: float) -> str:
    """Format a float for CSV output. Rejects NaN — the loader can't read it."""
    if value != value:  # NaN check (NaN is the only float not equal to itself)
        raise ValueError("Plan values cannot be NaN")
    return repr(value)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedRow:
    """Intermediate shape held while building the tree. Not part of the API."""

    path: tuple[str, ...]
    weights: tuple[float, ...]
    volatility: Optional[float]
    adjustment: float
    row_number: int


def read_plan_csv(stream: TextIO) -> list[CategoryNode]:
    """Parse `stream` as a leaves-only interleaved plan CSV.

    Returns a list of root `CategoryNode`s with intermediate branches
    reconstructed from the per-level weights repeated across sibling
    leaves. Branches get `volatility=None` and `adjustment=1.0` (the
    loader's defaults — the leaf still carries the resolved volatility
    written by `write_plan_csv`).

    Raises `PlanCSVError` on:
    - a malformed header,
    - a row with non-contiguous level cells (gaps inside the path),
    - a row whose level/weight cells disagree on filled-vs-blank,
    - a leaf path that duplicates another row,
    - the same intermediate path appearing with two different per-level
      weights across rows (the conflict check),
    - a non-numeric weight, or
    - a volatility/adjustment that fails the same parse as the YAML loader.

    Sibling-weight totals are NOT checked here — that is delegated to
    `build_portfolio_plan_from_nodes` so the same validator covers both
    `plan import` and `plan validate`.
    """
    reader = csv.reader(stream)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise PlanCSVError("CSV is empty") from exc

    level_columns = _parse_header(header)
    expected_cells = level_columns * CELLS_PER_LEVEL + len(TRAILING_COLUMNS)

    parsed_rows: list[_ParsedRow] = []
    for row_number, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            # Trailing/embedded blank rows are common when a spreadsheet
            # leaves empty rows below the data. Skip silently.
            continue
        parsed_rows.append(
            _parse_row(
                raw_row,
                expected_cells=expected_cells,
                level_columns=level_columns,
                row_number=row_number,
            )
        )

    # Reject duplicate leaf paths up-front (clearer error than the
    # conflict-on-leaf check below would produce).
    seen_paths: dict[tuple[str, ...], int] = {}
    for parsed in parsed_rows:
        previous = seen_paths.get(parsed.path)
        if previous is not None:
            raise PlanCSVError(
                f"duplicate leaf path '{_format_path(parsed.path)}' "
                f"(first appeared at row {previous})",
                row_number=parsed.row_number,
            )
        seen_paths[parsed.path] = parsed.row_number

    # Reject leaves under leaves (one path is a strict prefix of another).
    # Sort by depth so any conflict is noticed when the deeper row appears.
    sorted_paths = sorted(seen_paths.keys(), key=len)
    for index, path in enumerate(sorted_paths):
        for other in sorted_paths[index + 1 :]:
            if other[: len(path)] == path:
                raise PlanCSVError(
                    f"leaf '{_format_path(path)}' has a deeper leaf "
                    f"'{_format_path(other)}' under it — leaves cannot have children",
                    row_number=seen_paths[other],
                )

    # Conflict check: every intermediate prefix of every leaf must agree on
    # its per-level weight across the rows that mention it. Walk each leaf's
    # path and pick up the (parent_path, child_name, weight, row_number)
    # tuples; reject the first disagreement.
    branch_weights: dict[tuple[tuple[str, ...], str], tuple[float, int]] = {}
    for parsed in parsed_rows:
        for depth in range(len(parsed.path)):
            parent_path = parsed.path[:depth]
            child_name = parsed.path[depth]
            child_weight = parsed.weights[depth]
            existing = branch_weights.get((parent_path, child_name))
            if existing is None:
                branch_weights[(parent_path, child_name)] = (
                    child_weight,
                    parsed.row_number,
                )
                continue
            previous_weight, previous_row = existing
            if not math.isclose(
                previous_weight,
                child_weight,
                rel_tol=0.0,
                abs_tol=WEIGHT_CONFLICT_TOLERANCE,
            ):
                full_path = _format_path(parent_path + (child_name,))
                raise PlanCSVError(
                    f"conflicting weight for '{full_path}': "
                    f"row {previous_row} says {previous_weight!r}, "
                    f"row {parsed.row_number} says {child_weight!r}",
                    row_number=parsed.row_number,
                )

    return _build_tree(parsed_rows)


def _build_tree(parsed_rows: Sequence[_ParsedRow]) -> list[CategoryNode]:
    """Construct the `CategoryNode` tree from validated parsed rows.

    Uses the first occurrence of each (parent_path, child_name) to set the
    branch's per-level weight; conflict detection has already happened in
    `read_plan_csv`. Branch nodes get `volatility=None` and the default
    adjustment of 1.0 — the loader will inherit the leaf's volatility back
    into the leaf at risk-math time, and adjustment never propagates.
    """
    nodes_by_path: dict[tuple[str, ...], CategoryNode] = {}
    roots: list[CategoryNode] = []

    for parsed in parsed_rows:
        # Materialise every ancestor on this leaf's path, then the leaf itself.
        for depth in range(len(parsed.path)):
            ancestor_path = parsed.path[: depth + 1]
            if ancestor_path in nodes_by_path:
                continue
            is_leaf = depth == len(parsed.path) - 1
            node = CategoryNode(
                name=ancestor_path[-1],
                weight=parsed.weights[depth],
                volatility=parsed.volatility if is_leaf else None,
                adjustment=parsed.adjustment if is_leaf else 1.0,
                children=[],
            )
            nodes_by_path[ancestor_path] = node
            if depth == 0:
                roots.append(node)
            else:
                parent = nodes_by_path[ancestor_path[:-1]]
                parent.children.append(node)
    return roots


def _format_path(path: Sequence[str]) -> str:
    """Render a path tuple in the project's canonical ` / ` form for messages."""
    return " / ".join(path) if path else "(root)"


def _parse_header(header: Sequence[str]) -> int:
    """Return the number of `(level, weight)` pairs. Raises on a malformed header.

    The header must be exactly `level1, weight1, level2, weight2, ...,
    levelN, weightN, volatility, adjustment` (in that order). Anything
    else is rejected so silent column reordering cannot lose data.
    """
    cleaned = [cell.strip() for cell in header]
    trailing_count = len(TRAILING_COLUMNS)
    if len(cleaned) < CELLS_PER_LEVEL + trailing_count:
        raise PlanCSVError(
            f"header must have at least one (level, weight) pair plus {','.join(TRAILING_COLUMNS)}",
            row_number=1,
        )
    trailing = tuple(cleaned[-trailing_count:])
    if trailing != TRAILING_COLUMNS:
        raise PlanCSVError(
            f"header must end with {','.join(TRAILING_COLUMNS)}, got {','.join(trailing)}",
            row_number=1,
        )
    prefix = cleaned[:-trailing_count]
    if len(prefix) % CELLS_PER_LEVEL != 0:
        raise PlanCSVError(
            f"header must have an even number of leading columns "
            f"(level/weight pairs), got {len(prefix)}",
            row_number=1,
        )
    pair_count = len(prefix) // CELLS_PER_LEVEL
    for depth in range(1, pair_count + 1):
        expected_level = f"{LEVEL_COLUMN_PREFIX}{depth}"
        expected_weight = f"{WEIGHT_COLUMN_PREFIX}{depth}"
        actual_level = prefix[(depth - 1) * CELLS_PER_LEVEL]
        actual_weight = prefix[(depth - 1) * CELLS_PER_LEVEL + 1]
        if actual_level != expected_level:
            raise PlanCSVError(
                f"header column {(depth - 1) * CELLS_PER_LEVEL + 1} must be "
                f"'{expected_level}', got '{actual_level}'",
                row_number=1,
            )
        if actual_weight != expected_weight:
            raise PlanCSVError(
                f"header column {(depth - 1) * CELLS_PER_LEVEL + 2} must be "
                f"'{expected_weight}', got '{actual_weight}'",
                row_number=1,
            )
    return pair_count


def _parse_row(
    row: Sequence[str],
    *,
    expected_cells: int,
    level_columns: int,
    row_number: int,
) -> _ParsedRow:
    """Convert one CSV row into a `_ParsedRow`. Raises `PlanCSVError` on failure.

    Rows shorter than the header are right-padded with empty cells (many
    spreadsheets trim trailing empties). Rows longer than the header are
    rejected so accidental extra columns don't get silently dropped.
    """
    if len(row) < expected_cells:
        padded = list(row) + [""] * (expected_cells - len(row))
    elif len(row) > expected_cells:
        raise PlanCSVError(
            f"row has {len(row)} cells but header expects {expected_cells}",
            row_number=row_number,
        )
    else:
        padded = list(row)

    level_cells: list[str] = []
    weight_cells: list[str] = []
    for depth in range(level_columns):
        level_cells.append(padded[depth * CELLS_PER_LEVEL].strip())
        weight_cells.append(padded[depth * CELLS_PER_LEVEL + 1].strip())
    volatility_cell = padded[level_columns * CELLS_PER_LEVEL].strip()
    adjustment_cell = padded[level_columns * CELLS_PER_LEVEL + 1].strip()

    # Validate path/weight cell pairing: a level cell and its sibling weight
    # cell must be both filled or both blank, and filled cells must form a
    # contiguous prefix from level1.
    path_length = 0
    seen_blank = False
    for depth in range(level_columns):
        level = level_cells[depth]
        weight = weight_cells[depth]
        if level and weight:
            if seen_blank:
                raise PlanCSVError(
                    "category names must be contiguous from level1 (no gaps)",
                    row_number=row_number,
                )
            path_length += 1
        elif not level and not weight:
            seen_blank = True
        else:
            raise PlanCSVError(
                f"level{depth + 1} and weight{depth + 1} must both be filled or both be blank",
                row_number=row_number,
            )

    if path_length == 0:
        raise PlanCSVError("row has no category name", row_number=row_number)

    path = tuple(level_cells[:path_length])
    parsed_weights: list[float] = []
    for depth in range(path_length):
        try:
            parsed_weights.append(_parse_weight(weight_cells[depth]))
        except ValueError as exc:
            raise PlanCSVError(
                f"weight{depth + 1}: {exc}",
                row_number=row_number,
            ) from exc

    try:
        volatility = _parse_optional_volatility(volatility_cell or None)
    except ValueError as exc:
        raise PlanCSVError(str(exc), row_number=row_number) from exc

    try:
        adjustment = _parse_adjustment(adjustment_cell or None)
    except ValueError as exc:
        raise PlanCSVError(str(exc), row_number=row_number) from exc

    return _ParsedRow(
        path=path,
        weights=tuple(parsed_weights),
        volatility=volatility,
        adjustment=adjustment,
        row_number=row_number,
    )
