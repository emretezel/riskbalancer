from __future__ import annotations

"""
Configuration helpers for RiskBalancer category hierarchies.

Author: Emre Tezel
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Union

import yaml

from .models import CategoryPath, CategoryTarget
from .portfolio import PortfolioPlan


Scalar = Union[float, int, str]


def _parse_weight(raw: Optional[Scalar]) -> float:
    """Parse percentages/fractions into float weights."""
    if raw is None:
        raise ValueError("Weight value is required")
    if isinstance(raw, (float, int)):
        value = float(raw)
    else:
        cleaned = raw.strip()
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1].strip()
            value = float(cleaned) / 100.0
        else:
            value = float(cleaned)
    if value < 0 or value > 1:
        raise ValueError("Weights must be between 0 and 1 (inclusive)")
    return value


def _parse_optional_volatility(raw: Optional[Scalar]) -> Optional[float]:
    """Parse optional volatility entries into floats (if provided)."""
    if raw is None:
        return None
    if isinstance(raw, (float, int)):
        value = float(raw)
    else:
        value = float(raw.strip())
    return value if value > 0 else None


def _parse_adjustment(raw: Optional[Scalar]) -> float:
    """Parse adjustment factors; fallback to 1.0 if blank."""
    if raw is None:
        return 1.0
    if isinstance(raw, (float, int)):
        value = float(raw)
    else:
        value = float(raw.strip())
    if value < 0:
        raise ValueError("adjustment must be non-negative")
    return value or 1.0


@dataclass
class CategoryNode:
    """Represents a configurable category with children."""

    name: str
    weight: float
    volatility: Optional[float] = None
    adjustment: float = 1.0
    children: list["CategoryNode"] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping) -> "CategoryNode":
        """Create a node (and its children) from dictionary data."""
        children_raw = data.get("children") or []
        children = [cls.from_mapping(child) for child in children_raw]
        return cls(
            name=str(data["name"]),
            weight=_parse_weight(data.get("weight")),
            volatility=_parse_optional_volatility(data.get("volatility")),
            adjustment=_parse_adjustment(data.get("adjustment")),
            children=children,
        )

    def validate(self, tolerance: float = 1e-6) -> None:
        if not self.children:
            return
        total = sum(child.weight for child in self.children)
        if abs(total - 1.0) > tolerance:
            raise ValueError(f"Children of {self.name} must sum to 1, got {total}")
        for child in self.children:
            child.validate(tolerance)

    def collect_leaf_data(
        self,
        *,
        prefix: Sequence[str] = (),
        parent_weight: float = 1.0,
        default_leaf_volatility: float = 0.15,
        inherited_volatility: Optional[float] = None,
        accumulator: List[dict],
    ) -> None:
        path = tuple(prefix) + (self.name,)
        absolute_weight = parent_weight * self.weight
        current_volatility = self.volatility or inherited_volatility
        if self.children:
            for child in self.children:
                child.collect_leaf_data(
                    prefix=path,
                    parent_weight=absolute_weight,
                    default_leaf_volatility=default_leaf_volatility,
                    inherited_volatility=current_volatility,
                    accumulator=accumulator,
                )
        else:
            volatility = current_volatility or default_leaf_volatility
            if volatility <= 0:
                raise ValueError(f"Leaf category {self.name} needs a positive volatility")
            risk_weight = absolute_weight * self.adjustment
            accumulator.append(
                {
                    "path": path,
                    "weight": absolute_weight,
                    "risk_weight": risk_weight,
                    "volatility": volatility,
                    "adjustment": self.adjustment,
                }
            )


def load_category_nodes_from_yaml(path: Union[str, Path]) -> list[CategoryNode]:
    """Load hierarchical category nodes from a YAML file."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not data:
        raise ValueError("Category configuration YAML is empty")
    assets_data = data.get("assets", data)
    if not isinstance(assets_data, list):
        raise ValueError("Category configuration must define an 'assets' list")
    nodes = [CategoryNode.from_mapping(entry) for entry in assets_data]
    return nodes


def load_portfolio_plan_from_yaml(
    path: Union[str, Path],
    *,
    tolerance: float = 2e-2,
    default_leaf_volatility: float = 0.15,
) -> PortfolioPlan:
    """Build a PortfolioPlan by flattening YAML category definitions."""
    nodes = load_category_nodes_from_yaml(path)
    total_top = sum(node.weight for node in nodes)
    if abs(total_top - 1.0) > tolerance:
        raise ValueError(f"Top level assets must sum to 1, got {total_top}")
    for node in nodes:
        node.validate(tolerance)
    leaf_data = []
    for node in nodes:
        node.collect_leaf_data(
            prefix=(),
            parent_weight=1.0,
            default_leaf_volatility=default_leaf_volatility,
            inherited_volatility=None,
            accumulator=leaf_data,
        )
    risk_total = sum(item["risk_weight"] for item in leaf_data)
    if risk_total <= 0:
        raise ValueError("Total risk weight must be positive")
    category_targets = [
        CategoryTarget(
            path=CategoryPath(item["path"]),
            normalized_risk_weight=item["risk_weight"] / risk_total,
            volatility=item["volatility"],
            risk_weight=item["risk_weight"],
            adjustment=item.get("adjustment", 1.0),
        )
        for item in leaf_data
    ]
    return PortfolioPlan(targets=category_targets, tolerance=tolerance)
