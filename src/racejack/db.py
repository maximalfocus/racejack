"""Connection handling for the one shared PostgreSQL store.

The store is genuinely shared: both application replicas open their own pool against the same
database. That is what makes this demonstration honest — an invariant held only inside one process
is not held at all once a second process writes the same rows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

Row = dict[str, Any]
Conn = psycopg.AsyncConnection[Row]
ConnPool = AsyncConnectionPool[Conn]


def make_pool(database_url: str, *, min_size: int = 2, max_size: int = 16) -> ConnPool:
    """Build a closed pool; the caller opens it inside its own lifespan."""
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
    # The row factory above is what makes every connection from this pool a `Conn`; the pool's own
    # signature cannot express that, so it is asserted here once rather than at every call site.
    return cast(ConnPool, pool)


@asynccontextmanager
async def connect(database_url: str) -> AsyncIterator[Conn]:
    """One short-lived connection, for setup work that runs outside the application."""
    conn = await psycopg.AsyncConnection.connect(
        database_url, row_factory=dict_row, autocommit=True
    )
    try:
        yield conn
    finally:
        await conn.close()
