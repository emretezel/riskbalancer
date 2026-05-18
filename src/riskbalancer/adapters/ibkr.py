"""
Interactive Brokers CSV adapter for RiskBalancer.

The adapter emits positions in their native currency — IBKR statements
report USD, EUR, GBP, etc. depending on the holding. FX conversion to GBP
happens at report time using the `fx_rate` table; the adapter does not
need any FX rates injected.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional, Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class IBKRCSVAdapter(StatementAdapter):
    """Adapter that parses Interactive Brokers MTM CSV exports."""

    source_name = "Interactive Brokers CSV"

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return self.parse_file(handle)

    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        reader = csv.reader(handle)
        in_positions = False
        investments: list[Investment] = []
        rows = list(reader)
        idx = 0

        while idx < len(rows):
            row = rows[idx]
            section = row[0] if row else ""
            if section == "Positions and Mark-to-Market Profit and Loss":
                kind = row[1]
                if kind == "Header":
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
            idx += 1
        return investments

    def _parse_position_row(self, row: Sequence[str]) -> Optional[Investment]:
        if len(row) < 18:
            return None
        discriminator = row[2]
        currency = (row[4] or "").strip().upper() or "GBP"
        symbol = (row[5] or "").strip()
        description = (row[6] or "").strip()
        if not symbol or not description:
            return None
        if discriminator != "Summary":
            return None
        market_value = self._parse_number(row[12])
        return Investment(
            instrument_id=symbol or description,
            description=description or symbol,
            market_value=market_value,
            currency=currency,
            source="ibkr",
        )

    @staticmethod
    def _parse_number(value: str) -> float:
        sanitized = value.replace(",", "").strip()
        if not sanitized:
            return 0.0
        return float(sanitized)
