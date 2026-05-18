"""
Citibank Holdings CSV adapter for RiskBalancer.

The export is USD-denominated; the adapter emits positions in their native
USD and lets the report layer convert to GBP via the `fx_rate` table.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class CitiCSVAdapter(StatementAdapter):
    """Adapter for Citibank holdings exports."""

    source_name = "Citi CSV"

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
            investments.append(
                Investment(
                    instrument_id=symbol or description,
                    description=description or symbol,
                    market_value=market_value,
                    currency="USD",
                    source="citi",
                )
            )
        return investments

    @staticmethod
    def _parse_currency(value: str | None) -> float:
        if not value:
            return 0.0
        sanitized = value.replace("$", "").replace(",", "").strip()
        if not sanitized:
            return 0.0
        return float(sanitized)
