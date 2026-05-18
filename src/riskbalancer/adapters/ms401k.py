"""
Morgan Stanley 401(k) CSV adapter for RiskBalancer.

USD-denominated balances; the adapter emits positions natively and the
report layer handles the GBP conversion.

Author: Emre Tezel
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence, TextIO, Union

from ..models import Investment
from .base import StatementAdapter


class MS401KCSVAdapter(StatementAdapter):
    """Adapter for Morgan Stanley 401(k) statements."""

    source_name = "MS 401k CSV"

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
            if closing_balance == 0:
                continue
            instrument_id = description.replace(" ", "_")
            investments.append(
                Investment(
                    instrument_id=instrument_id,
                    description=description,
                    market_value=closing_balance,
                    currency="USD",
                    source="ms401k",
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
