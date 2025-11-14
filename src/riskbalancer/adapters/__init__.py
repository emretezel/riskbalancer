"""Adapters used to normalize broker statements into Investment objects."""

from .aj_bell import AJBellCSVAdapter
from .base import StatementAdapter

__all__ = ["AJBellCSVAdapter", "StatementAdapter"]
