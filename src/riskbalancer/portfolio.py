"""
Portfolio plan container.

The `PortfolioPlan` is the in-memory shape the report consumes: a flat list of
`CategoryTarget` rows, one per plan-leaf, with target weight + intrinsic
volatility + adjustment. The richer `Portfolio`/`PortfolioAnalyzer` pair was
removed when positions migrated to the SQLite `position` table — aggregation
and risk-parity computation now live in the report command directly, reading
from the `current_position` view.

Author: Emre Tezel
"""

from __future__ import annotations

from typing import Dict, Iterable, Sequence

from .models import CategoryPath, CategoryTarget


class PortfolioPlan:
    """Holds the desired target allocation across the hierarchy."""

    def __init__(self, targets: Iterable[CategoryTarget], tolerance: float = 1e-6):
        self._targets: Dict[CategoryPath, CategoryTarget] = {
            target.path: target for target in targets
        }
        if not self._targets:
            raise ValueError("PortfolioPlan requires at least one category target")
        self.tolerance = tolerance
        self._validate_targets()

    def __iter__(self):
        return iter(self._targets.values())

    def __len__(self) -> int:
        return len(self._targets)

    def get(self, path: CategoryPath) -> CategoryTarget:
        return self._targets[path]

    @property
    def targets(self) -> Sequence[CategoryTarget]:
        return list(self._targets.values())

    def _validate_targets(self) -> None:
        total_weight = sum(target.target_weight for target in self._targets.values())
        if abs(total_weight - 1.0) > self.tolerance:
            raise ValueError("Total target weight across categories must sum to 1")

        seen_paths = set()
        for target in self._targets.values():
            if target.path in seen_paths:
                raise ValueError(f"Duplicate category path specified: {target.path.label()}")
            seen_paths.add(target.path)
