"""Create the schema and (re)seed the fictional fixtures.

Seeding is *setup*, not observation: it is the only place in the project that touches the database
directly on behalf of the demonstration. Every claim the demonstration goes on to make is read back
through the application's own HTTP boundary instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import fixtures, schema
from .config import RunnerConfig
from .db import Conn, connect
from .instrumentation import create_instrumentation, drop_instrumentation


async def create_schema(conn: Conn) -> None:
    """Create every table, index, and constraint. Idempotent."""
    await conn.execute(schema.CREATE_SCHEMA)


async def reset_fixtures(conn: Conn) -> None:
    """Discard all state and rebuild the fictional fixtures exactly as they are defined."""
    async with conn.transaction():
        await conn.execute(schema.TRUNCATE_ALL)
        await conn.cursor().executemany(
            "INSERT INTO buyers (buyer_id, display_name) VALUES (%s, %s)",
            [(buyer.buyer_id, buyer.display_name) for buyer in fixtures.BUYERS],
        )
        await conn.execute(
            "INSERT INTO drops (drop_id, product_name, units_available, units_sold)"
            " VALUES (%s, %s, %s, 0)",
            (fixtures.DROP_ID, fixtures.DROP_PRODUCT_NAME, fixtures.DROP_UNITS_AVAILABLE),
        )
        await conn.execute(
            "INSERT INTO wallets (wallet_id, owner_buyer_id, balance_cents) VALUES (%s, %s, 0)",
            (fixtures.WALLET_ID, fixtures.WALLET_OWNER_BUYER_ID),
        )
        await conn.execute(
            "INSERT INTO credit_codes (code, amount_cents) VALUES (%s, %s)",
            (fixtures.CREDIT_CODE, fixtures.CREDIT_CODE_AMOUNT_CENTS),
        )


async def seed(database_url: str, *, create: bool = True) -> None:
    async with connect(database_url) as conn:
        if create:
            await create_schema(conn)
        await reset_fixtures(conn)


async def present_backstops(conn: Conn) -> set[str]:
    """Which of the two named backstop constraints the store currently carries."""
    cursor = await conn.execute(
        schema.PRESENT_BACKSTOP_CONSTRAINTS, (list(schema.BACKSTOP_CONSTRAINTS),)
    )
    return {str(row["conname"]) for row in await cursor.fetchall()}


async def prepare_secure_run(database_url: str) -> None:
    """Fresh fixtures, the backstop in place, and no instrumentation anywhere."""
    async with connect(database_url) as conn:
        await create_schema(conn)
        await reset_fixtures(conn)
        # Order matters: the fixtures must be consistent again before the constraints can be
        # re-added, since a previous vulnerable run may have left an oversold drop behind.
        await conn.execute(schema.RESTORE_BACKSTOP_CONSTRAINTS)
        await drop_instrumentation(conn)


async def prepare_vulnerable_run(database_url: str) -> None:
    """Fresh fixtures, the backstop deliberately removed, instrumentation tables available.

    Removing the backstop is what lets the application-level damage show instead of surfacing as
    a constraint violation. It is done by name, for vulnerable runs only, and the output says so.
    """
    async with connect(database_url) as conn:
        await create_schema(conn)
        await reset_fixtures(conn)
        await conn.execute(schema.DROP_BACKSTOP_CONSTRAINTS)
        await create_instrumentation(conn)


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="racejack-seed",
        description="Create the racejack schema and reset its fictional fixtures.",
    )
    parser.add_argument(
        "--fixtures-only",
        action="store_true",
        help="reset fixture rows without re-running the schema DDL",
    )
    parser.add_argument(
        "--secure",
        action="store_true",
        help=(
            "restore the secure baseline: fresh fixtures, both backstop constraints back in place, "
            "and no instrumentation tables. Use after a vulnerable run."
        ),
    )
    args = parser.parse_args(argv)
    config = RunnerConfig.from_env()
    if args.secure:
        await prepare_secure_run(config.database_url)
        print("secure baseline restored: backstop constraints in place, no instrumentation")
        return 0
    await seed(config.database_url, create=not args.fixtures_only)
    print(
        f"seeded {fixtures.STORE_NAME}: drop {fixtures.DROP_ID} "
        f"({fixtures.DROP_UNITS_AVAILABLE} units), "
        f"code {fixtures.CREDIT_CODE} ({fixtures.CREDIT_CODE_AMOUNT_CENTS} cents), "
        f"wallet {fixtures.WALLET_ID}, {len(fixtures.BUYERS)} fictional buyers"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
