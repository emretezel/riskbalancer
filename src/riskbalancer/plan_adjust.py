"""
Convenience interface for updating leaf `adjustment` values on a user's
`plan.yaml`.

The CLI command `rb plan adjust` exposes three mutually exclusive modes:

- a depth-first **walker** over every leaf with `weight > 0`, prompting
  per leaf for a new adjustment;
- a **targeted** single-leaf set keyed by category path;
- a read-only **`--list`** dump of every leaf (zero-weight leaves
  included, for context).

All write paths flow back into `plan_bootstrap.write_plan_yaml`, which
performs the atomic write back to `private/users/<user>/plan.yaml`. The
CLI glue lives in `cli.cmd_plan_adjust`; this module owns the pure
logic and the interactive walker so they can be unit-tested without a
real terminal via `plan_bootstrap.ScriptedIO`.

Author: Emre Tezel
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from .configuration import CategoryNode

# `_ask` and `_prompt_yes_no` are private helpers in `plan_bootstrap`, but
# they are the canonical abort-aware prompt primitives used by every
# interactive command in the project. Importing them here keeps the
# `ScriptedIO` test seam and the quit/exit/Ctrl+C/EOF behaviour identical
# between `plan create` and `plan adjust` — a duplicate copy would drift.
from .plan_bootstrap import IO, PlanCreationAborted, _ask, _prompt_yes_no

__all__ = [
    "LeafChange",
    "PlanCreationAborted",
    "iter_leaf_nodes",
    "filter_under",
    "apply_targeted",
    "walk_adjustments",
    "render_diff",
    "render_list",
    "confirm_changes",
    "normalize_under",
]


@dataclass(frozen=True)
class LeafChange:
    """A single adjustment update produced by walker or targeted edit.

    `path` is the full category-path tuple to the affected leaf; `old`
    and `new` are the adjustment values before and after the change. The
    object is informational — the underlying `CategoryNode.adjustment`
    has already been mutated by the producer when this is constructed.
    Used by `render_diff` and by tests that need a stable handle on
    what the walker decided.
    """

    path: tuple[str, ...]
    old: float
    new: float


def _label_for(path: Sequence[str]) -> str:
    """Render a category-path tuple as the project's canonical label."""
    return " / ".join(path)


def normalize_under(raw: str) -> str:
    """Normalise an `--under` argument into the canonical separator form.

    Accepts both `>` (the doc/help-text convention used in user-facing
    examples) and `/` (what `_normalize_label` and `_parse_category_label`
    in `cli.py` consume). The result is whitespace-trimmed, joined with
    ` / `, and lower-cased so a single prefix match handles both
    spellings.
    """
    canonical = raw.replace(">", "/")
    parts = [part.strip() for part in canonical.split("/") if part.strip()]
    return " / ".join(parts).lower()


def iter_leaf_nodes(
    nodes: Sequence[CategoryNode],
    *,
    prefix: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], CategoryNode]]:
    """Yield `(path, node)` for every leaf, depth-first, in plan order.

    A leaf is a `CategoryNode` whose `children` list is empty. The path
    tuple contains the leaf's full ancestor chain including its own name,
    so callers can render `Bonds / Developed / UK / Govt` without needing
    to thread the ancestor stack themselves.
    """
    for node in nodes:
        node_path = prefix + (node.name,)
        if node.children:
            yield from iter_leaf_nodes(node.children, prefix=node_path)
        else:
            yield node_path, node


def filter_under(
    leaves: Iterable[tuple[tuple[str, ...], CategoryNode]],
    under: Optional[str],
) -> list[tuple[tuple[str, ...], CategoryNode]]:
    """Restrict `leaves` to those whose path lies under the given subtree.

    The match is a normalised prefix check with the separator suffix
    appended — so `--under "Bonds"` matches `Bonds / Developed / UK / Govt`
    but does **not** match a hypothetical sibling whose name happens to
    start with the same letters (e.g. `BondsInflationLinked`). Returns
    the full list unchanged when `under` is None or empty.

    Raises `ValueError` when `under` is non-empty but matches zero leaves
    so the CLI can show a clear error and a few candidate paths.
    """
    materialised = list(leaves)
    if not under:
        return materialised
    needle = normalize_under(under)
    if not needle:
        return materialised
    needle_prefix = needle + " / "
    matched = [
        (path, node)
        for path, node in materialised
        if _label_for(path).lower().startswith(needle_prefix)
    ]
    if not matched:
        candidates = ", ".join(_label_for(path) for path, _ in materialised[:5])
        raise ValueError(f"--under '{under}' did not match any leaf. Nearby paths: {candidates}")
    return matched


def _find_child(nodes: Sequence[CategoryNode], name: str) -> Optional[CategoryNode]:
    """Return the child whose name matches `name` (case- and whitespace-insensitive)."""
    needle = name.strip().lower()
    for child in nodes:
        if child.name.strip().lower() == needle:
            return child
    return None


def apply_targeted(
    nodes: Sequence[CategoryNode],
    path_parts: Sequence[str],
    new_adjustment: float,
) -> LeafChange:
    """Update the adjustment of the leaf at `path_parts`, mutating in place.

    Used by the non-interactive `rb plan adjust <path> <value>` form.
    Raises `ValueError` for an empty path, a negative value, a path that
    doesn't resolve, or a path that lands on a branch (since adjustments
    live only on leaves).
    """
    if new_adjustment < 0:
        raise ValueError("adjustment must be non-negative")
    if not path_parts:
        raise ValueError("empty category path")

    cursor: Sequence[CategoryNode] = nodes
    node: Optional[CategoryNode] = None
    for segment in path_parts:
        node = _find_child(cursor, segment)
        if node is None:
            raise ValueError(f"Unknown category path '{_label_for(path_parts)}'")
        cursor = node.children
    # The loop body executes at least once because we rejected empty paths
    # above, so `node` is always assigned by the time we get here.
    assert node is not None

    if node.children:
        raise ValueError(
            f"'{_label_for(path_parts)}' is a branch, not a leaf — adjustments live on leaves only"
        )
    old = node.adjustment
    node.adjustment = new_adjustment
    return LeafChange(path=tuple(path_parts), old=old, new=new_adjustment)


def walk_adjustments(
    leaves: Sequence[tuple[tuple[str, ...], CategoryNode]],
    io: IO,
) -> list[LeafChange]:
    """Walk `leaves` and prompt for a new adjustment on each non-zero-weight node.

    Skips leaves whose `weight == 0` silently — they are explicitly out of
    the risk-parity calculation, so prompting for an adjustment is noise.
    Per leaf, the user can:

    - press Enter to keep the current value (no `LeafChange` emitted);
    - type a non-negative number to replace it (mutates `node.adjustment`
      and records a `LeafChange` only when the value actually differs);
    - type `q` to stop walking and proceed straight to the diff/confirm
      step with whatever changes have accumulated so far.

    `quit`, `exit`, Ctrl+C, and EOF route through `_ask` and raise
    `PlanCreationAborted` — same abort path as `plan create`, so the CLI
    can use one `except` block for both commands.
    """
    eligible = [(path, node) for path, node in leaves if node.weight > 0]
    total = len(eligible)
    changes: list[LeafChange] = []

    if total == 0:
        io.info("No leaves with positive weight to adjust.")
        return changes

    for index, (path, node) in enumerate(eligible, start=1):
        vol_text = f"{node.volatility}" if node.volatility is not None else "—"
        io.info(
            f"\n[{index}/{total}] {_label_for(path)}\n"
            f"  weight={node.weight}  vol={vol_text}  adjustment={node.adjustment}"
        )
        while True:
            raw = _ask(io, "  New adjustment (blank=keep, q=stop): ").strip()
            if raw == "":
                break
            if raw.lower() == "q":
                return changes
            try:
                value = float(raw)
            except ValueError:
                io.warn("Enter a non-negative number, blank to keep, or q to stop.")
                continue
            if value < 0:
                io.warn("Adjustment must be non-negative.")
                continue
            if value != node.adjustment:
                changes.append(LeafChange(path=path, old=node.adjustment, new=value))
                node.adjustment = value
            break
    return changes


def render_diff(changes: Sequence[LeafChange]) -> str:
    """Render the pending adjustment changes as an aligned `old → new` table."""
    if not changes:
        return "(no changes)"
    path_width = max(len(_label_for(change.path)) for change in changes)
    path_width = max(path_width, len("PATH"))
    lines = [f"{'PATH':<{path_width}}   OLD  →  NEW"]
    for change in changes:
        label = _label_for(change.path)
        lines.append(f"{label:<{path_width}}   {change.old}  →  {change.new}")
    return "\n".join(lines)


def render_list(
    leaves: Sequence[tuple[tuple[str, ...], CategoryNode]],
) -> str:
    """Render a full leaf table including zero-weight entries (read-only view).

    Unlike `walk_adjustments`, this listing keeps zero-weight leaves
    visible so the user can see the entire plan at a glance.
    """
    materialised = list(leaves)
    if not materialised:
        return "(no leaves)"
    path_width = max(len(_label_for(path)) for path, _ in materialised)
    path_width = max(path_width, len("PATH"))
    header = f"{'PATH':<{path_width}}  {'WEIGHT':>7}  {'VOL':>7}  {'ADJ':>5}"
    out = [header]
    for path, node in materialised:
        vol_text = f"{node.volatility}" if node.volatility is not None else "—"
        out.append(
            f"{_label_for(path):<{path_width}}  "
            f"{node.weight:>7}  {vol_text:>7}  {node.adjustment:>5}"
        )
    return "\n".join(out)


def confirm_changes(
    plan_path: Path,
    changes: Sequence[LeafChange],
    io: IO,
    *,
    skip_prompt: bool = False,
) -> bool:
    """Show the diff and prompt y/N before persisting `changes`.

    Returns True when the caller should proceed with the write, False when
    the user declined (with `skip_prompt=True` always returns True). The
    underlying `_prompt_yes_no` still raises `PlanCreationAborted` on
    quit/exit/Ctrl+C/EOF, so the same abort path as `plan create` applies.
    """
    if not changes:
        io.info("(no changes)")
        return False
    io.info(render_diff(changes))
    if skip_prompt:
        return True
    return _prompt_yes_no(
        io,
        f"\nApply these changes to {plan_path}? [y/N]: ",
        default=False,
    )
