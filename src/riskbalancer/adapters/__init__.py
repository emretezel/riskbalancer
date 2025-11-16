"""
Adapters used to normalize broker statements into Investment objects.

Author: Emre Tezel
"""

from .aj_bell import AJBellCSVAdapter
from .base import StatementAdapter
from .ibkr import IBKRCSVAdapter

__all__ = ["AJBellCSVAdapter", "IBKRCSVAdapter", "StatementAdapter"]
