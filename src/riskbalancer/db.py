"""
Database connection and lifecycle for RiskBalancer.

A single SQLite database at `private/riskbalancer.db` is the working store
for every mutable concept in the project — users, categories, plans,
mappings, statement imports, positions, FX history. The committed YAML
files under `config/` remain as one-shot seed inputs only.

`Database.connect()` is the only sanctioned way to open the database:
it sets `PRAGMA foreign_keys = ON` so referential integrity is actually
enforced, switches journalling to WAL for crash safety on file-backed
databases, and applies any pending schema migrations. Pass `:memory:` to
open an ephemeral database for tests.

Author: Emre Tezel
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Optional, Union

from .migrations import apply_migrations

# Minimum SQLite version we depend on. `STRICT` table syntax landed in
# 3.37.0 and the GLOB date checks in our CHECK constraints are available
# everywhere we care about. Python 3.12's bundled sqlite3 satisfies this
# on every supported platform; we still verify at connect time so a stale
# system SQLite produces a clear error rather than a confusing parse failure.
_MIN_SQLITE_VERSION = (3, 37, 0)


class Database:
    """Thin wrapper around `sqlite3.Connection` enforcing project conventions.

    Construct via `Database.connect(path)`. The wrapper is intentionally
    small — repositories take the underlying `sqlite3.Connection` directly
    so they can use ordinary parameterised queries. The wrapper exists
    primarily to centralise PRAGMA setup, migration application, and
    teardown.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def connect(cls, path: Union[str, Path]) -> "Database":
        """Open the database at `path`, apply migrations, return the wrapper.

        - File paths: the parent directory is created on demand. The DB
          uses WAL journalling for crash safety.
        - `:memory:`: an ephemeral in-process database, used by tests.

        Raises `RuntimeError` if the installed SQLite is too old to support
        the schema, or if the on-disk DB has a higher schema version than
        the running binary knows how to handle (i.e. the user has
        downgraded the application).
        """
        _require_modern_sqlite()
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        # `isolation_level=None` puts pysqlite in autocommit mode; we issue
        # explicit BEGIN/COMMIT in the migration runner so we get atomic
        # multi-statement migrations without pysqlite second-guessing us.
        connection = sqlite3.connect(target, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if target == ":memory:":
            # WAL is meaningless for an in-memory DB; pick the cheapest
            # journal mode so tests don't pay for fsync.
            connection.execute("PRAGMA journal_mode = MEMORY")
        else:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        apply_migrations(connection)
        return cls(connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the underlying sqlite3 connection for repository use."""
        return self._connection

    def close(self) -> None:
        """Close the underlying connection. Safe to call repeatedly."""
        self._connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


def _require_modern_sqlite() -> None:
    """Refuse to run against a SQLite older than 3.37 (STRICT table support).

    The schema declares every table `STRICT` so column types are actually
    enforced; that syntax landed in 3.37.0. A clear error here beats a
    cryptic "near 'STRICT': syntax error" from the first migration.
    """
    version = sqlite3.sqlite_version_info
    if version < _MIN_SQLITE_VERSION:
        installed = ".".join(str(part) for part in version)
        required = ".".join(str(part) for part in _MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"SQLite {required} or newer is required (found {installed}). "
            "Upgrade your Python installation or its bundled SQLite."
        )
