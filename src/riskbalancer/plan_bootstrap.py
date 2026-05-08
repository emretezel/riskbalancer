"""
Plan bootstrap: build a new user's `plan.yaml` from the catalog of categories
the system already knows about.

The catalog is derived at runtime by unioning every visible peer-user plan,
the committed `config/seed_plan.yaml`, and the leaf paths referenced by every
file in `config/mappings/`. The interactive walker then lets the user pick,
at every level of the tree, which categories to keep and how to weight them.

Author: Emre Tezel
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Sequence

import yaml

from .configuration import (
    CategoryNode,
    collect_category_weight_validation_failures,
    format_category_weight_validation_failures,
    load_category_nodes_from_yaml,
)
from .paths import UserPaths

DEFAULT_LEAF_VOLATILITY = 0.15
DEFAULT_ADJUSTMENT = 1.0


@dataclass
class CatalogNode:
    """A single option in the bootstrap catalog.

    Unlike `CategoryNode`, the catalog is descriptive (a menu of choices) not
    prescriptive (a plan that must validate). `suggested_weight`,
    `suggested_volatility`, and `suggested_adjustment` are informational hints
    drawn from whichever source the node was first seen in. `from_mappings`
    flags leaves that exist only because a mapping file references them.
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


def build_catalog(paths: UserPaths) -> list[CatalogNode]:
    """Build the merged catalog of categories the system already knows.

    Priority order (highest first): peer-user plans, the committed
    `config/seed_plan.yaml`, and finally mapping-leaf paths that no plan has
    yet included. When a node appears in multiple sources, the
    highest-priority source wins for `suggested_volatility` and
    `suggested_adjustment`; child sets are unioned in first-seen order so the
    user is offered every option that has ever been described.
    """
    catalog: list[CatalogNode] = []
    for source_nodes in _peer_plan_sources(paths):
        _merge_nodes_into_catalog(source_nodes, catalog)
    if paths.seed_plan.exists():
        _merge_nodes_into_catalog(load_category_nodes_from_yaml(paths.seed_plan), catalog)
    for leaf_path in _collect_mapping_leaves(paths):
        _ensure_leaf_in_catalog(catalog, leaf_path)
    return catalog


def _peer_plan_sources(paths: UserPaths) -> Iterable[list[CategoryNode]]:
    """Yield CategoryNode lists for every visible peer-user plan."""
    if not paths.users_root.exists():
        return
    for user_dir in sorted(paths.users_root.iterdir()):
        if not user_dir.is_dir():
            continue
        if user_dir.name == paths.user:
            continue  # the user being created has no plan yet
        peer_plan = user_dir / "plan.yaml"
        if peer_plan.exists():
            yield load_category_nodes_from_yaml(peer_plan)


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


def _collect_mapping_leaves(paths: UserPaths) -> list[tuple[str, ...]]:
    """Return every category path referenced by any adapter mapping file.

    Pulls from the shared `config/mappings/` directory and the user's own
    overrides directory. Paths are returned as tuples of components in
    deterministic insertion order.
    """
    leaves: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    candidate_dirs = [paths.shared_mappings_dir, paths.overrides_dir]
    for directory in candidate_dirs:
        if not directory.exists():
            continue
        for mapping_file in sorted(directory.glob("*.yaml")):
            data = yaml.safe_load(mapping_file.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            for payload in data.values():
                if not isinstance(payload, dict):
                    continue
                allocations = payload.get("allocations") or []
                if not allocations and payload.get("category"):
                    allocations = [payload["category"]]
                for entry in allocations:
                    if isinstance(entry, str):
                        label = entry
                    elif isinstance(entry, dict):
                        label = entry.get("category", "")
                    else:
                        continue
                    parts = tuple(p.strip() for p in label.split("/") if p.strip())
                    if not parts or parts in seen:
                        continue
                    seen.add(parts)
                    leaves.append(parts)
    return leaves


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


def walk_catalog_interactive(
    catalog: Sequence[CatalogNode],
    io: IO,
    *,
    default_leaf_volatility: float = DEFAULT_LEAF_VOLATILITY,
) -> list[CategoryNode]:
    """Run the recursive pick-one/weight/add-another loop and return a plan."""
    if not catalog:
        raise ValueError(
            "Catalog is empty: no plan files or mappings are visible. "
            "Add at least one plan or seed_plan.yaml before running plan create."
        )
    return _walk_level(
        list(catalog),
        io,
        level_label="top level",
        path_prefix=(),
        default_leaf_volatility=default_leaf_volatility,
    )


def _walk_level(
    options: list[CatalogNode],
    io: IO,
    *,
    level_label: str,
    path_prefix: tuple[str, ...],
    default_leaf_volatility: float,
    inherited_volatility: Optional[float] = None,
) -> list[CategoryNode]:
    io.info(f"\n—— {level_label} ——")
    picked: list[tuple[CatalogNode, float]] = []
    while True:
        picked_ids = {id(node) for node, _ in picked}
        remaining = [node for node in options if id(node) not in picked_ids]
        if not remaining:
            break
        chosen = _prompt_pick_one(io, remaining, level_label, picked)
        weight = _prompt_weight(io, chosen, level_label)
        picked.append((chosen, weight))
        if not _prompt_add_another(io, level_label, len(picked) == 1):
            break
    if not picked:
        raise ValueError(f"At least one asset class must be added at {level_label}")
    _validate_level_weights(picked, io, level_label)

    plan_nodes: list[CategoryNode] = []
    for catalog_node, weight in picked:
        node_path = path_prefix + (catalog_node.name,)
        node_label = " / ".join(node_path)
        next_inherited = catalog_node.suggested_volatility or inherited_volatility
        if catalog_node.children:
            child_plan = _walk_level(
                list(catalog_node.children),
                io,
                level_label=node_label,
                path_prefix=node_path,
                default_leaf_volatility=default_leaf_volatility,
                inherited_volatility=next_inherited,
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
                inherited_volatility=next_inherited,
                default_leaf_volatility=default_leaf_volatility,
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


def _prompt_pick_one(
    io: IO,
    remaining: list[CatalogNode],
    level_label: str,
    picked: list[tuple[CatalogNode, float]],
) -> CatalogNode:
    labels = [_decorate_label(node) for node in remaining]
    name_to_node = {node.name.lower(): node for node in remaining}
    progress = (
        ", ".join(f"{node.name}={int(round(weight * 100))}%" for node, weight in picked)
        if picked
        else "none yet"
    )
    while True:
        raw = io.prompt(
            f"Select an asset class to add to {level_label} "
            f"[{', '.join(labels)}] (assigned so far: {progress}): "
        )
        cleaned = raw.strip().lower()
        if cleaned in name_to_node:
            return name_to_node[cleaned]
        io.warn(f"Unknown asset class '{raw.strip()}'. Choose one of the listed options.")


def _decorate_label(node: CatalogNode) -> str:
    if node.from_mappings:
        return f"{node.name} (from mappings)"
    return node.name


def _prompt_weight(io: IO, chosen: CatalogNode, level_label: str) -> float:
    suggestion = (
        f" (catalog suggests {int(round((chosen.suggested_weight or 0) * 100))}%)"
        if chosen.suggested_weight is not None
        else ""
    )
    while True:
        raw = io.prompt(f"Risk weight for {chosen.name} at {level_label}{suggestion}: ")
        try:
            return _parse_weight_input(raw)
        except ValueError as exc:
            io.warn(str(exc))


def _prompt_add_another(io: IO, level_label: str, only_one_so_far: bool) -> bool:
    raw = io.prompt(f"Add another asset class to {level_label}? [y/N]: ").strip().lower()
    if raw in {"y", "yes"}:
        return True
    if raw in {"", "n", "no"}:
        return False
    io.warn("Please answer y or n.")
    return _prompt_add_another(io, level_label, only_one_so_far)


def _validate_level_weights(
    picked: list[tuple[CatalogNode, float]], io: IO, level_label: str
) -> None:
    """Ensure the entered weights at this level sum to 100%; re-prompt on failure.

    Builds an artificial CategoryNode list and runs the existing validator so
    the same tolerance applies as for the final plan check.
    """
    while True:
        artificial = [
            CategoryNode(name=node.name, weight=weight, volatility=0.1) for node, weight in picked
        ]
        failures = collect_category_weight_validation_failures(artificial)
        if not failures:
            io.info(f"✓ {level_label} sums to 100%")
            return
        io.warn(format_category_weight_validation_failures(failures))
        io.info(f"Re-enter the weights for {level_label}:")
        for index, (node, _weight) in enumerate(picked):
            new_weight = _prompt_weight(io, node, level_label)
            picked[index] = (node, new_weight)


def _prompt_leaf_metadata(
    io: IO,
    node_label: str,
    catalog_node: CatalogNode,
    *,
    inherited_volatility: Optional[float],
    default_leaf_volatility: float,
) -> tuple[float, float]:
    suggested_vol = (
        catalog_node.suggested_volatility
        if catalog_node.suggested_volatility is not None
        else inherited_volatility
        if inherited_volatility is not None
        else default_leaf_volatility
    )
    suggested_adj = (
        catalog_node.suggested_adjustment
        if catalog_node.suggested_adjustment is not None
        else DEFAULT_ADJUSTMENT
    )
    volatility = _prompt_positive_float(
        io,
        f"Volatility for {node_label} [catalog suggests {suggested_vol}]: ",
        default=suggested_vol,
    )
    adjustment = _prompt_positive_float(
        io,
        f"Adjustment for {node_label} [catalog suggests {suggested_adj}]: ",
        default=suggested_adj,
    )
    return volatility, adjustment


def _prompt_positive_float(io: IO, message: str, *, default: float) -> float:
    while True:
        raw = io.prompt(message).strip()
        if not raw:
            return default
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError("value must be positive")
            return value
        except ValueError:
            io.warn("Enter a positive number, or press Enter to accept the suggested value.")


_WEIGHT_RE = re.compile(r"^\s*([0-9.]+)\s*%?\s*$")


def _parse_weight_input(raw: str) -> float:
    match = _WEIGHT_RE.match(raw)
    if not match:
        raise ValueError(
            f"Invalid weight '{raw}'. Use a percentage (e.g. 55) or fraction (e.g. 0.55)."
        )
    value = float(match.group(1))
    if value > 1:
        value /= 100.0
    if value <= 0:
        raise ValueError("Weights must be positive")
    if value > 1.0 + 1e-9:
        raise ValueError("Weights must not exceed 100%")
    return value


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_plan_yaml(path: Path, nodes: Sequence[CategoryNode]) -> None:
    """Serialise a plan tree to YAML in the canonical `assets:` shape."""
    payload = {"assets": [_node_to_dict(node) for node in nodes]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _node_to_dict(node: CategoryNode) -> dict:
    payload: dict = {"name": node.name, "weight": node.weight}
    if node.volatility is not None:
        payload["volatility"] = node.volatility
    if node.adjustment != DEFAULT_ADJUSTMENT:
        payload["adjustment"] = node.adjustment
    if node.children:
        payload["children"] = [_node_to_dict(child) for child in node.children]
    return payload


def clone_plan(source: UserPaths, target: UserPaths) -> None:
    """Copy `source.plan` to `target.plan` and validate the result.

    Raises FileNotFoundError if the source plan does not exist; raises
    ValueError if the cloned plan fails weight validation (which would only
    happen if the source plan was already invalid).
    """
    if not source.plan.exists():
        raise FileNotFoundError(f"Source plan {source.plan} does not exist")
    target.plan.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source.plan, target.plan)
    nodes = load_category_nodes_from_yaml(target.plan)
    failures = collect_category_weight_validation_failures(nodes)
    if failures:
        raise ValueError(format_category_weight_validation_failures(failures))


# ---------------------------------------------------------------------------
# Catalog helpers used by the CLI
# ---------------------------------------------------------------------------


def describe_catalog_sources(paths: UserPaths) -> str:
    """Render a one-line summary of which sources fed the catalog."""
    parts: List[str] = []
    if paths.users_root.exists():
        peers = [
            user_dir.name
            for user_dir in sorted(paths.users_root.iterdir())
            if (
                user_dir.is_dir()
                and user_dir.name != paths.user
                and (user_dir / "plan.yaml").exists()
            )
        ]
        if peers:
            parts.append(f"{len(peers)} peer plan(s): {', '.join(peers)}")
    if paths.seed_plan.exists():
        parts.append("config/seed_plan.yaml")
    mapping_count = sum(
        1
        for directory in (paths.shared_mappings_dir, paths.overrides_dir)
        if directory.exists()
        for _ in directory.glob("*.yaml")
    )
    if mapping_count:
        parts.append(f"{mapping_count} adapter mapping file(s)")
    if not parts:
        return "Catalog: empty"
    return "Catalog: " + ", ".join(parts)


def count_unique_categories(catalog: Sequence[CatalogNode]) -> int:
    """Total node count across the entire catalog tree (for telemetry only)."""
    total = 0
    for node in catalog:
        total += 1
        total += count_unique_categories(node.children)
    return total
