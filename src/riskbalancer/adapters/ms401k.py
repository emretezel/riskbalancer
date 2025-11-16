from __future__ import annotations

"""
Morgan Stanley 401(k) CSV adapter for RiskBalancer.

Author: Emre Tezel
"""

import csv
from pathlib import Path
from typing import Optional, Sequence, TextIO, Union

from ..models import CategoryPath, Investment
from .base import StatementAdapter


class MS401KCSVAdapter(StatementAdapter):
    """Adapter for Morgan Stanley 401(k) statements.

    The export contains USD-denominated balances; we convert them using the
    provided FX rates (USD->GBP).
    """

    def __init__(
        self,
        *,
        default_category: Optional[CategoryPath] = None,
        default_volatility: float = 0.2,
        fx_rates: Optional[dict[str, float]] = None,
    ):
        super().__init__("MS 401k CSV")
        self.default_category = default_category or CategoryPath("Uncategorized", "Pending Review")
        self.default_volatility = default_volatility
        self.fx_rates = {k.upper(): v for k, v in (fx_rates or {}).items()}

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.DictReader(handle)
        investments: list[Investment] = []
        for row in reader:
            plan = row.get("Plan")
            if not plan:
                continue
            description = row.get("Fund Name", "").strip()
            if not description:
                continue
            closing_balance = self._parse_currency(row.get("Closing Balance", ""))
            usd_value = closing_balance
            gbp_value = self._convert_to_gbp("USD", usd_value)
            instrument_id = description.replace(" ", "_")
            investments.append(
                Investment(
                    instrument_id=instrument_id,
                    description=description,
                    market_value=gbp_value,
                    category=self.default_category,
                    volatility=self.default_volatility,
                    source="ms401k",
                )
            )
        return investments

    def _convert_to_gbp(self, currency: str, value: float) -> float:
        if currency == "GBP":
            return value
        rate = self.fx_rates.get(currency)
        if rate is None:
            raise ValueError(
                f"Missing FX rate for {currency}. Ensure config/fx.yaml contains entries for the statement currencies."
            )
        return value * rate

    @staticmethod
    def _parse_currency(value: str | None) -> float:
        if not value:
            return 0.0
        sanitized = value.replace("$", "").replace(",", "").strip()
        if not sanitized:
            return 0.0
        return float(sanitized)
