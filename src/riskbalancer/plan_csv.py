"""
CSV import/export for a user's category plan.

The on-disk source of truth is `plan.yaml` (nested categories with weights,
volatility, and adjustment). This module mirrors that tree in a flat,
spreadsheet-friendly CSV with one column per depth level plus the trailing
`weight, volatility, adjustment` triple. The shape lets users edit a whole
plan in a spreadsheet without going through the interactive walker.

Round-trip discipline: writing a tree and reading it back must reproduce the
same `CategoryNode` structure, including sibling order. The reader is
order-agnostic in input (rows can be shuffled in the file) but parents are
always assembled before their children, and sibling order falls back to the
input row order so an unedited round-trip is byte-stable when re-serialised
through `write_plan_yaml`.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
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

# Trailing column names (in canonical order) and the prefix used for the
# variable-width "level1, level2, ..." header columns. Centralised so the
# header writer and parser cannot drift.
WEIGHT_COLUMN = "weight"
VOLATILITY_COLUMN = "volatility"
ADJUSTMENT_COLUMN = "adjustment"
TRAILING_COLUMNS: tuple[str, ...] = (WEIGHT_COLUMN, VOLATILITY_COLUMN, ADJUSTMENT_COLUMN)
LEVEL_COLUMN_PREFIX = "level"


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
    """Render `nodes` as a depth-column CSV onto `stream`.

    Header is `level1,...,levelN,weight,volatility,adjustment` where N is
    the maximum depth of the tree. Each row represents one node, written
    in DFS pre-order so parents always appear before their children.
    Volatility cells are blank when the node has no value (matching the
    `volatility: null` shape of the YAML); adjustment is always written
    explicitly so the CSV is self-describing in a spreadsheet.

    Float formatting uses `repr` so values round-trip byte-identically
    through `read_plan_csv` for an unedited export.
    """
    writer = csv.writer(stream)
    max_depth = _max_depth(nodes)
    if max_depth == 0:
        # Empty plan — emit just the trailing-column header. The reader will
        # reject this on `build_portfolio_plan_from_nodes` (root totals 0%);
        # we still write a parseable file so the export does not silently
        # produce an empty file with no header.
        writer.writerow(list(TRAILING_COLUMNS))
        return
    header = [f"{LEVEL_COLUMN_PREFIX}{idx}" for idx in range(1, max_depth + 1)]
    header.extend(TRAILING_COLUMNS)
    writer.writerow(header)
    for path, node in _iter_dfs(nodes):
        row: list[str] = []
        for level_index in range(max_depth):
            # Cells beyond the node's own depth stay blank; this is what
            # signals "this row defines a node at depth len(path)".
            row.append(path[level_index] if level_index < len(path) else "")
        row.append(_format_number(node.weight))
        row.append("" if node.volatility is None else _format_number(node.volatility))
        row.append(_format_number(node.adjustment))
        writer.writerow(row)


def _max_depth(nodes: Sequence[CategoryNode]) -> int:
    """Return the deepest path length in the tree (0 for an empty list)."""
    deepest = 0
    for node in nodes:
        deepest = max(deepest, 1 + _max_depth(node.children))
    return deepest


def _iter_dfs(
    nodes: Sequence[CategoryNode],
    *,
    prefix: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], CategoryNode]]:
    """Yield `(path, node)` for every node in DFS pre-order, including branches."""
    for node in nodes:
        node_path = prefix + (node.name,)
        yield node_path, node
        if node.children:
            yield from _iter_dfs(node.children, prefix=node_path)


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
    weight: float
    volatility: Optional[float]
    adjustment: float
    row_number: int


def read_plan_csv(stream: TextIO) -> list[CategoryNode]:
    """Parse `stream` as a plan CSV into a list of root `CategoryNode`s.

    The reader is order-agnostic: rows are sorted by `(depth, original_index)`
    so a parent is always assembled before any of its children, but sibling
    order from the source CSV is preserved. Validation mirrors the YAML
    loader's: weights go through `_parse_weight` (so percent suffixes and
    0..1 fractions are both accepted), and empty volatility/adjustment cells
    fall back to the same defaults (`volatility=None`, `adjustment=1.0`).

    Raises `PlanCSVError` for a malformed header, a duplicate path, a
    missing parent, a non-numeric weight, or any other structural problem.
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
    parsed_rows: list[_ParsedRow] = []
    for row_number, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            # Trailing blank rows are common when a spreadsheet adds empty
            # rows below the data. Skip silently rather than fail.
            continue
        parsed_rows.append(
            _parse_row(
                raw_row,
                header_len=len(header),
                level_columns=level_columns,
                row_number=row_number,
            )
        )

    # Sort by depth so parents are always processed before children. Within
    # one depth, preserve original CSV order so siblings keep their order.
    parsed_rows.sort(key=lambda parsed: (len(parsed.path), parsed.row_number))

    nodes_by_path: dict[tuple[str, ...], CategoryNode] = {}
    roots: list[CategoryNode] = []
    for parsed in parsed_rows:
        if parsed.path in nodes_by_path:
            raise PlanCSVError(
                f"duplicate path '{_format_path(parsed.path)}'",
                row_number=parsed.row_number,
            )
        node = CategoryNode(
            name=parsed.path[-1],
            weight=parsed.weight,
            volatility=parsed.volatility,
            adjustment=parsed.adjustment,
            children=[],
        )
        nodes_by_path[parsed.path] = node
        if len(parsed.path) == 1:
            roots.append(node)
            continue
        parent_path = parsed.path[:-1]
        parent = nodes_by_path.get(parent_path)
        if parent is None:
            raise PlanCSVError(
                f"parent '{_format_path(parent_path)}' is not defined",
                row_number=parsed.row_number,
            )
        parent.children.append(node)
    return roots


def _format_path(path: Sequence[str]) -> str:
    """Render a path tuple in the project's canonical ` / ` form for messages."""
    return " / ".join(path)


def _parse_header(header: Sequence[str]) -> int:
    """Return the number of `level*` columns. Raises on a malformed header.

    The header must be exactly `level1, level2, ..., levelN, weight,
    volatility, adjustment` (in that order). Anything else is rejected so
    silent column reordering cannot lose data.
    """
    cleaned = [cell.strip() for cell in header]
    trailing_count = len(TRAILING_COLUMNS)
    if len(cleaned) < trailing_count + 1:
        raise PlanCSVError(
            f"header must have at least one level column plus {','.join(TRAILING_COLUMNS)}",
            row_number=1,
        )
    trailing = tuple(cleaned[-trailing_count:])
    if trailing != TRAILING_COLUMNS:
        raise PlanCSVError(
            f"header must end with {','.join(TRAILING_COLUMNS)}, got {','.join(trailing)}",
            row_number=1,
        )
    level_cells = cleaned[:-trailing_count]
    for expected_index, cell in enumerate(level_cells, start=1):
        expected = f"{LEVEL_COLUMN_PREFIX}{expected_index}"
        if cell != expected:
            raise PlanCSVError(
                f"header column {expected_index} must be '{expected}', got '{cell}'",
                row_number=1,
            )
    return len(level_cells)


def _parse_row(
    row: Sequence[str],
    *,
    header_len: int,
    level_columns: int,
    row_number: int,
) -> _ParsedRow:
    """Convert one CSV row into a `_ParsedRow`. Raises `PlanCSVError` on failure.

    Rows shorter than the header are right-padded with empty cells (many
    spreadsheets trim trailing empties). Rows longer than the header are
    rejected so accidental extra columns don't get silently dropped.
    """
    if len(row) < header_len:
        padded = list(row) + [""] * (header_len - len(row))
    elif len(row) > header_len:
        raise PlanCSVError(
            f"row has {len(row)} cells but header expects {header_len}",
            row_number=row_number,
        )
    else:
        padded = list(row)

    level_cells = [cell.strip() for cell in padded[:level_columns]]
    weight_cell = padded[level_columns].strip()
    volatility_cell = padded[level_columns + 1].strip()
    adjustment_cell = padded[level_columns + 2].strip()

    path = tuple(cell for cell in level_cells if cell)
    if not path:
        raise PlanCSVError("row has no category name", row_number=row_number)
    # The depth-column shape only allows trailing empties to indicate a
    # shallower node — gaps inside the path (e.g. `Equities, , NAM`) or a
    # missing root (`'', Equities`) are silent ways to lose hierarchy and
    # must be rejected. Non-empty cells must form a contiguous prefix.
    if not all(level_cells[: len(path)]):
        raise PlanCSVError(
            "category names must be contiguous from level1 (no gaps)",
            row_number=row_number,
        )

    if not weight_cell:
        raise PlanCSVError("weight is required", row_number=row_number)
    try:
        weight = _parse_weight(weight_cell)
    except ValueError as exc:
        raise PlanCSVError(str(exc), row_number=row_number) from exc

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
        weight=weight,
        volatility=volatility,
        adjustment=adjustment,
        row_number=row_number,
    )
