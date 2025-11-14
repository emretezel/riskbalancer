from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, TextIO, Union

from ..models import CategoryPath, Investment
from .base import StatementAdapter


class AJBellCSVAdapter(StatementAdapter):
    """Adapter that parses AJ Bell CSV statements."""

    def __init__(
        self,
        *,
        default_category: Optional[CategoryPath] = None,
        default_volatility: float = 0.2,
    ):
        super().__init__("AJ Bell CSV")
        self.default_category = default_category or CategoryPath(
            "Uncategorized", "Pending Review"
        )
        self.default_volatility = default_volatility

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
        quantity = row.get("Quantity")
        if not name or not value_raw:
            return None

        market_value = self._parse_number(value_raw)
        if market_value == 0:
            return None

        resolved_category = self.default_category
        volatility = self.default_volatility
        quantity_value = self._parse_optional_number(quantity)

        return Investment(
            instrument_id=ticker or name,
            description=name,
            market_value=market_value,
            quantity=quantity_value,
            category=resolved_category,
            volatility=volatility,
            source="aj_bell",
        )

    @staticmethod
    def _parse_number(value: str) -> float:
        sanitized = value.replace(",", "").replace("£", "").replace("Â", "").strip()
        sanitized = sanitized.replace("%", "")
        if not sanitized:
            return 0.0
        return float(sanitized)

    @classmethod
    def _parse_optional_number(cls, value: Optional[str]) -> Optional[float]:
        if value is None or not value.strip():
            return None
        return cls._parse_number(value)

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
