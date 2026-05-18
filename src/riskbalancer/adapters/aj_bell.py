"""
AJ Bell statement adapter for RiskBalancer.

AJ Bell exports are GBP-only. The adapter emits positions in their native
currency (GBP) and lets the report layer handle any FX conversion later.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class AJBellCSVAdapter(StatementAdapter):
    """Adapter that parses AJ Bell CSV statements."""

    source_name = "AJ Bell CSV"

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.DictReader(handle)
        investments: list[Investment] = []
        for row in reader:
            investment = self._row_to_investment(row)
            if investment:
                investments.append(investment)
        return investments

    def parse_rows(self, rows: Iterable[dict[str, str]]) -> Sequence[Investment]:
        investments: list[Investment] = []
        for row in rows:
            investment = self._row_to_investment(row)
            if investment:
                investments.append(investment)
        return investments

    def _row_to_investment(self, row: Mapping[str, str]) -> Optional[Investment]:
        name = row.get("Investment")
        value_raw = self._get_first(row, ["Value (£)", "Value (Â£)", "Value (£ )", "Value"])
        ticker = row.get("Ticker") or row.get("Symbol")
        if not name or not value_raw:
            return None

        market_value = self._parse_number(value_raw)
        if market_value == 0:
            return None

        return Investment(
            instrument_id=ticker or name,
            description=name,
            market_value=market_value,
            currency="GBP",
            source="aj_bell",
        )

    @staticmethod
    def _parse_number(value: str) -> float:
        sanitized = value.replace(",", "").replace("£", "").replace("Â", "").strip()
        sanitized = sanitized.replace("%", "")
        if not sanitized:
            return 0.0
        return float(sanitized)

    @staticmethod
    def _get_first(row: Mapping[str, str], keys: Sequence[str]) -> Optional[str]:
        for key in keys:
            if key in row:
                return row[key]
        # attempt case-insensitive fallback
        lowered = {k.lower(): v for k, v in row.items()}
        for key in keys:
            normalized = key.lower()
            if normalized in lowered:
                return lowered[normalized]
        return None
