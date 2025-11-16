from __future__ import annotations

"""
Interactive Brokers CSV adapter for RiskBalancer.

Author: Emre Tezel
"""

import csv
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, TextIO, Union

from ..models import CategoryPath, Investment
from .base import StatementAdapter


class IBKRCSVAdapter(StatementAdapter):
    """Adapter that parses Interactive Brokers MTM CSV exports and converts to GBP."""

    def __init__(
        self,
        *,
        default_category: Optional[CategoryPath] = None,
        default_volatility: float = 0.2,
        fx_rates: Optional[Mapping[str, float]] = None,
    ):
        super().__init__("Interactive Brokers CSV")
        self.default_category = default_category or CategoryPath("Uncategorized", "Pending Review")
        self.default_volatility = default_volatility
        self.fx_rates = {k.upper(): v for k, v in (fx_rates or {}).items()}

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.reader(handle)
        in_positions = False
        header: Sequence[str] | None = None
        investments: list[Investment] = []
        rows = list(reader)
        idx = 0

        while idx < len(rows):
            row = rows[idx]
            section = row[0] if row else ""
            if section == "Positions and Mark-to-Market Profit and Loss":
                kind = row[1]
                if kind == "Header":
                    header = row
                    in_positions = True
                    idx += 1
                    continue
                if in_positions and kind == "Data":
                    data = row
                    entry = self._parse_position_row(data)
                    if entry:
                        investments.append(entry)
            else:
                in_positions = False
                header = None
            idx += 1
        return investments

    def _parse_position_row(self, row: Sequence[str]) -> Optional[Investment]:
        if len(row) < 18:
            return None
        discriminator = row[2]
        asset_class = row[3]
        currency = (row[4] or "").strip().upper()
        symbol = (row[5] or "").strip()
        description = (row[6] or "").strip()
        if not symbol or not description:
            return None
        if discriminator != "Summary":
            return None
        market_value = self._parse_number(row[12])
        gbp_market_value = self._convert_to_gbp(currency, market_value)
        category = self.default_category
        return Investment(
            instrument_id=symbol or description,
            description=description or symbol,
            market_value=gbp_market_value,
            category=category,
            volatility=self.default_volatility,
            source="ibkr",
        )

    def _convert_to_gbp(self, currency: str, value: float) -> float:
        if currency in {"", "GBP"}:
            return value
        if not self.fx_rates:
            raise ValueError(
                f"FX rates are required to convert {currency} to GBP. Supply --fx when building the portfolio."
            )
        rate = self.fx_rates.get(currency)
        if rate is None:
            raise ValueError(f"Missing FX rate for {currency} in the supplied FX file")
        return value * rate

    @staticmethod
    def _parse_number(value: str) -> float:
        sanitized = value.replace(",", "").strip()
        if not sanitized:
            return 0.0
        return float(sanitized)
