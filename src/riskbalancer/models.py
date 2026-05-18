"""
Core dataclasses used throughout RiskBalancer.

Author: Emre Tezel
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple, Union


@dataclass(frozen=True)
class CategoryPath:
    """Represents a hierarchical category path of arbitrary depth."""

    parts: Tuple[str, ...]

    def __init__(self, *parts: Union[str, Iterable[str]]):
        if len(parts) == 1 and isinstance(parts[0], Iterable) and not isinstance(parts[0], str):
            normalized = tuple(str(part).strip() for part in parts[0] if str(part).strip())
        else:
            normalized = tuple(str(part).strip() for part in parts if str(part).strip())
        if not normalized:
            raise ValueError("CategoryPath requires at least one level")
        object.__setattr__(self, "parts", normalized)

    def __len__(self) -> int:
        return len(self.parts)

    def __iter__(self):
        return iter(self.parts)

    def levels(self) -> Tuple[str, ...]:
        return self.parts

    @property
    def level1(self) -> str:
        return self.parts[0]

    @property
    def level2(self) -> str:
        return self.parts[1] if len(self.parts) > 1 else ""

    @property
    def level3(self) -> str:
        return self.parts[2] if len(self.parts) > 2 else ""

    def parent_prefix(self, depth: int) -> Tuple[str, ...]:
        """Return tuple that identifies the parent up to the requested depth."""
        if depth < 0 or depth > len(self.parts):
            raise ValueError("depth must be between 0 and the path length")
        return self.parts[:depth]

    def label(self) -> str:
        """Human readable path."""
        return " / ".join(self.parts)


@dataclass(frozen=True)
class CategoryTarget:
    """Desired target risk allocation for a fully qualified category."""

    path: CategoryPath
    normalized_risk_weight: float
    volatility: float
    risk_weight: float
    adjustment: float = 1.0

    def __post_init__(self) -> None:
        if self.normalized_risk_weight < 0 or self.normalized_risk_weight > 1:
            raise ValueError("normalized_risk_weight must be between 0 and 1")
        if self.risk_weight < 0:
            raise ValueError("risk_weight must be non-negative")
        if self.volatility <= 0:
            raise ValueError("volatility must be positive")
        # `adjustment == 0` is permitted for leaves like seed `Cash` that
        # are catalogued but carry zero allocation: `risk_weight = weight *
        # adjustment` is well-defined at zero, and the leaf still needs a
        # `CategoryTarget` so the plan-index lookups (used by the portfolio
        # walker) can find it by path.
        if self.adjustment < 0:
            raise ValueError("adjustment must be non-negative")

    @property
    def target_weight(self) -> float:
        """Backward compatible alias for normalized risk weight."""
        return self.normalized_risk_weight


@dataclass
class Investment:
    """Normalized view of a single line item coming from a broker statement.

    `market_value` is in the position's native `currency` — no FX conversion
    happens at parse time. The database stores the native amount; GBP
    equivalents are derived at report time by joining `fx_rate`.
    """

    instrument_id: str
    description: str
    market_value: float
    currency: str = "GBP"
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.market_value < 0:
            raise ValueError("market_value must not be negative")
        normalised = self.currency.strip().upper()
        if len(normalised) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        self.currency = normalised


@dataclass(frozen=True)
class CategoryStatus:
    """Summary for a sub-category vs target/cash weight."""

    path: CategoryPath
    actual_weight: float
    target_cash_weight: float

    @property
    def delta(self) -> float:
        return self.actual_weight - self.target_cash_weight

    @property
    def status(self) -> str:
        if abs(self.delta) < 1e-4:
            return "on_target"
        return "over_invested" if self.delta > 0 else "under_invested"


def normalize_weights(weights: Iterable[float]) -> Tuple[float, ...]:
    total = sum(weights)
    if total <= 0:
        raise ValueError("sum of weights must be positive")
    return tuple(w / total for w in weights)
