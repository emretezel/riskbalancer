"""
Charles Schwab positions CSV adapter for RiskBalancer.

USD-denominated holdings; the adapter emits positions natively and the
report layer converts to GBP via the `fx_rate` table.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class SchwabCSVAdapter(StatementAdapter):
    """Adapter for Schwab positions exports."""

    source_name = "Schwab CSV"

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.reader(handle)
        rows = [row for row in reader if row]
        header_index = None
        for idx, row in enumerate(rows):
            if row and row[0] == "Symbol":
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
            symbol = (record.get("Symbol") or "").strip()
            description = (record.get("Description") or "").strip()
            if not symbol and not description:
                continue
            label = (symbol or description).strip().lower()
            if label in {"total", "account total"} or label.startswith("total "):
                continue
            market_value = self._parse_currency(
                record.get("Mkt Val (Market Value)") or record.get("Mtk Val (Market Value)", "")
            )
            if market_value == 0:
                continue
            investments.append(
                Investment(
                    instrument_id=symbol or description,
                    description=description or symbol,
                    market_value=market_value,
                    currency="USD",
                    source="schwab",
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
