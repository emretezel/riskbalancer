from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable, Sequence, TextIO, Union

from ..models import Investment


class StatementAdapter(ABC):
    """Base class for broker statement ingestion."""

    source_name: str

    def __init__(self, source_name: str):
        self.source_name = source_name

    def parse_path(self, path: Union[str, Path]) -> Sequence[Investment]:
        with open(path, "r", encoding="utf-8") as handle:
            return self.parse_file(handle)

    @abstractmethod
    def parse_file(self, handle: TextIO) -> Sequence[Investment]:
        """Return normalized investments found in the file."""

    def parse_rows(self, rows: Iterable[dict[str, str]]) -> Sequence[Investment]:
        """Optional helper for csv.DictReader driven adapters."""
        raise NotImplementedError
