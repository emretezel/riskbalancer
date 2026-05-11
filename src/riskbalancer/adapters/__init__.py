"""
Adapters used to normalize broker statements into Investment objects.

Author: Emre Tezel
"""

from .aegon import AegonCSVAdapter
from .aj_bell import AJBellCSVAdapter
from .base import StatementAdapter
from .citi import CitiCSVAdapter
from .ibkr import IBKRCSVAdapter
from .ms401k import MS401KCSVAdapter
from .schwab import SchwabCSVAdapter

__all__ = [
    "AJBellCSVAdapter",
    "AegonCSVAdapter",
    "CitiCSVAdapter",
    "IBKRCSVAdapter",
    "MS401KCSVAdapter",
    "SchwabCSVAdapter",
    "StatementAdapter",
]
