from __future__ import annotations

"""
Portfolio planning and analysis utilities for RiskBalancer.

Author: Emre Tezel
"""

from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence

from .models import (
    CategoryPath,
    CategoryStatus,
    CategoryTarget,
    Investment,
    normalize_weights,
)


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


class Portfolio:
    """Mutable container for normalized investments."""

    def __init__(self):
        self._investments: List[Investment] = []

    @property
    def investments(self) -> Sequence[Investment]:
        return tuple(self._investments)

    def add_investment(self, investment: Investment) -> None:
        self._investments.append(investment)

    def extend(self, investments: Iterable[Investment]) -> None:
        for investment in investments:
            self.add_investment(investment)

    def add_manual_investment(
        self,
        *,
        instrument_id: str,
        description: str,
        market_value: float,
        category: CategoryPath,
        volatility: float,
        quantity: Optional[float] = None,
    ) -> Investment:
        investment = Investment(
            instrument_id=instrument_id,
            description=description,
            market_value=market_value,
            quantity=quantity,
            category=category,
            volatility=volatility,
            source="manual",
        )
        self.add_investment(investment)
        return investment

    def total_value(self) -> float:
        return sum(inv.market_value for inv in self._investments)


class PortfolioAnalyzer:
    """Computes risk-parity cash weights and diagnostics for a portfolio."""

    def __init__(self, plan: PortfolioPlan, portfolio: Portfolio):
        self.plan = plan
        self.portfolio = portfolio

    def _aggregate_by_category(self) -> Dict[CategoryPath, float]:
        totals: Dict[CategoryPath, float] = defaultdict(float)
        for investment in self.portfolio.investments:
            totals[investment.category] += investment.market_value
        return totals

    def cash_weights(self) -> Dict[CategoryPath, float]:
        """Risk-parity cash weights derived from target weights and vol."""
        risk_units = []
        paths = []
        for target in self.plan:
            paths.append(target.path)
            risk_units.append(target.target_weight / target.volatility)
        normalized = normalize_weights(risk_units)
        return {path: weight for path, weight in zip(paths, normalized)}

    def category_status(self) -> List[CategoryStatus]:
        totals = self._aggregate_by_category()
        total_value = self.portfolio.total_value()
        if total_value <= 0:
            raise ValueError("Portfolio total value must be positive to compute weights")

        cash_weights = self.cash_weights()
        statuses: List[CategoryStatus] = []
        for target in self.plan:
            actual_weight = totals.get(target.path, 0.0) / total_value
            statuses.append(
                CategoryStatus(
                    path=target.path,
                    actual_weight=actual_weight,
                    target_cash_weight=cash_weights[target.path],
                )
            )
        return statuses
