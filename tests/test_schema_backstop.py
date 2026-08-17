"""The database-enforced backstop.

These two constraints are not the primary control — the application's atomic guards are — but they
must genuinely exist and genuinely hold, because their whole purpose is to be there when application
code is wrong. So this module writes to the store directly, on purpose, and asserts the database
refuses it.
"""

from __future__ import annotations

import uuid

import pytest
from psycopg import errors as pg_errors

from racejack import fixtures, schema
from racejack.config import RunnerConfig
from racejack.db import connect

pytestmark = pytest.mark.usefixtures("fresh_state")


async def test_both_backstop_constraints_exist(config: RunnerConfig) -> None:
    async with connect(config.database_url) as conn:
        cursor = await conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
            (list(schema.BACKSTOP_CONSTRAINTS),),
        )
        found = {str(row["conname"]) for row in await cursor.fetchall()}
    assert found == set(schema.BACKSTOP_CONSTRAINTS)


async def test_the_counter_check_constraint_refuses_an_oversold_drop(
    config: RunnerConfig,
) -> None:
    async with connect(config.database_url) as conn:
        with pytest.raises(pg_errors.CheckViolation):
            await conn.execute(
                "UPDATE drops SET units_sold = units_available + 1 WHERE drop_id = %s",
                (fixtures.DROP_ID,),
            )


async def test_the_uniqueness_constraint_refuses_a_second_redemption_of_one_code(
    config: RunnerConfig,
) -> None:
    insert = (
        "INSERT INTO redemptions"
        " (redemption_id, code, wallet_id, buyer_id, amount_cents, served_by)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
    )
    row = (
        fixtures.CREDIT_CODE,
        fixtures.WALLET_ID,
        fixtures.WALLET_OWNER_BUYER_ID,
        fixtures.CREDIT_CODE_AMOUNT_CENTS,
        "test",
    )
    async with connect(config.database_url) as conn:
        await conn.execute(insert, (uuid.uuid4(), *row))
        with pytest.raises(pg_errors.UniqueViolation):
            await conn.execute(insert, (uuid.uuid4(), *row))


async def test_a_wallet_cannot_go_negative(config: RunnerConfig) -> None:
    async with connect(config.database_url) as conn:
        with pytest.raises(pg_errors.CheckViolation):
            await conn.execute(
                "UPDATE wallets SET balance_cents = -1 WHERE wallet_id = %s",
                (fixtures.WALLET_ID,),
            )
