"""
Aegon pension statement adapter for RiskBalancer.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class AegonCSVAdapter(StatementAdapter):
    """Adapter that parses Aegon pension CSV statements.

    The export is GBP-only and groups holdings under a ``Section`` column
    (e.g. ``Main Portfolio``, ``BRSP Transfer In``). A ``TOTAL`` summary row
    sits between sections; it is skipped here because it carries no holding.
    """

    source_name = "Aegon CSV"

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        # ``utf-8-sig`` matches the other CSV adapters and tolerates a BOM
        # if the export was produced on Windows.
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
        name = (row.get("Investment") or "").strip()
        if not name:
            return None
        # Aegon emits a per-section ``TOTAL`` row with a blank ``Value``.
        # Drop it explicitly so it never reaches the portfolio.
        if name.upper() == "TOTAL":
            return None

        value_raw = row.get("Value")
        if not value_raw:
            return None

        market_value = self._parse_number(value_raw)
        if market_value == 0:
            return None

        return Investment(
            instrument_id=name,
            description=name,
            market_value=market_value,
            currency="GBP",
            source="aegon",
        )

    @staticmethod
    def _parse_number(value: str) -> float:
        sanitized = value.replace(",", "").replace("£", "").replace("Â", "").strip()
        sanitized = sanitized.replace("%", "")
        if not sanitized:
            return 0.0
        return float(sanitized)
