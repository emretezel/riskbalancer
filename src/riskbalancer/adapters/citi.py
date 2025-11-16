from __future__ import annotations

"""
Citibank Holdings CSV adapter for RiskBalancer.

Author: Emre Tezel
"""

import csv
from pathlib import Path
from typing import Optional, Sequence, TextIO, Union

from ..models import CategoryPath, Investment
from .base import StatementAdapter


class CitiCSVAdapter(StatementAdapter):
    """Adapter for Citibank holdings exports (converts USD to GBP)."""

    def __init__(
        self,
        *,
        default_category: Optional[CategoryPath] = None,
        default_volatility: float = 0.2,
        fx_rates: Optional[dict[str, float]] = None,
    ):
        super().__init__("Citi CSV")
        self.default_category = default_category or CategoryPath("Uncategorized", "Pending Review")
        self.default_volatility = default_volatility
        self.fx_rates = {k.upper(): v for k, v in (fx_rates or {}).items()}

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.reader(handle)
        rows = [row for row in reader if row]
        header_index = None
        for idx, row in enumerate(rows):
            if row and row[0].startswith("Security ID"):
                header_index = idx
                break
        if header_index is None:
            return []
        header = rows[header_index]
        data_rows = rows[header_index + 1 :]
        investments: list[Investment] = []
        for row in data_rows:
            if len(row) != len(header):
                continue
            record = dict(zip(header, row))
            symbol = (record.get("Security ID") or "").strip()
            description = (record.get("Description") or "").strip()
            if not symbol and not description:
                continue
            market_value = self._parse_currency(record.get("Market Value", ""))
            if market_value == 0:
                continue
            gbp_value = self._convert_to_gbp("USD", market_value)
            investments.append(
                Investment(
                    instrument_id=symbol or description,
                    description=description or symbol,
                    market_value=gbp_value,
                    category=self.default_category,
                    volatility=self.default_volatility,
                    source="citi",
                )
            )
        return investments

    def _convert_to_gbp(self, currency: str, value: float) -> float:
        if currency.upper() == "GBP":
            return value
        rate = self.fx_rates.get(currency.upper())
        if rate is None:
            raise ValueError(f"Missing FX rate for {currency}. Please update config/fx.yaml.")
        return value * rate

    @staticmethod
    def _parse_currency(value: str | None) -> float:
        if not value:
            return 0.0
        sanitized = value.replace("$", "").replace(",", "").strip()
        if not sanitized:
            return 0.0
        return float(sanitized)
