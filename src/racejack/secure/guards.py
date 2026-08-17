"""The three secure strategies, written as explicit SQL.

Each one closes the window between the check and the act, and each closes it a different way:

* **Strategy A — atomic conditional write.** The check *is* the write. One `UPDATE`
  carries the availability predicate in its `WHERE` clause, and the outcome is decided from the
  affected row count. No read of the counter feeds the decision, so there is no interval for a
  concurrent request to occupy.

* **Strategy B — transactional pessimistic guard.** Inside one transaction, every writer
  of the invariant first takes a row-level lock on the drop (`SELECT ... FOR UPDATE`), so the
  read-decide-write sequence that *looks* like the vulnerable one is in fact serialized against
  every other writer of the same row.

* **Strategy C — uniqueness-enforced single-use redemption.** The redemption is inserted
  against a `UNIQUE` constraint and the wallet is credited in the *same* transaction, so a second
  redemption fails as a constraint violation and takes its own credit down with it. Nothing reads
  whether the code was already redeemed.

The trade-off between A and B is worth stating plainly: the conditional write holds no lock and
needs one round trip, but requires the invariant to be expressible as a predicate; the pessimistic
guard serializes writers on the row and admits arbitrary decision logic in between.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

from psycopg import errors as pg_errors

from ..config import CounterGuard
from ..db import Conn
from ..models import OrderOutcome, OrderResult, RedemptionOutcome, RedemptionResult


class _UnknownTargetError(Exception):
    """Raised inside a transaction so it unwinds; never reaches the client."""


# --- Strategy A: the check is the write -------------------------------------------------------

SQL_CLAIM_UNIT_CONDITIONAL_WRITE: Final = """
UPDATE drops
   SET units_sold = units_sold + 1
 WHERE drop_id = %(drop_id)s
   AND units_sold < units_available
RETURNING units_sold
"""

SQL_DROP_EXISTS: Final = "SELECT 1 FROM drops WHERE drop_id = %(drop_id)s"

# --- Strategy B: serialize every writer on the row --------------------------------------------

SQL_LOCK_DROP: Final = """
SELECT units_available, units_sold
  FROM drops
 WHERE drop_id = %(drop_id)s
   FOR UPDATE
"""

SQL_INCREMENT_UNITS_SOLD: Final = """
UPDATE drops SET units_sold = units_sold + 1 WHERE drop_id = %(drop_id)s
"""

SQL_INSERT_ORDER: Final = """
INSERT INTO orders (order_id, drop_id, buyer_id, units, served_by)
VALUES (%(order_id)s, %(drop_id)s, %(buyer_id)s, 1, %(served_by)s)
"""

# --- Strategy C: credit and record are one transaction, uniqueness decides ---------------------

SQL_CREDIT_WALLET: Final = """
UPDATE wallets w
   SET balance_cents = w.balance_cents + c.amount_cents
  FROM credit_codes c
 WHERE w.wallet_id = %(wallet_id)s
   AND c.code = %(code)s
RETURNING w.balance_cents AS balance_cents, c.amount_cents AS amount_cents
"""

SQL_INSERT_REDEMPTION: Final = """
INSERT INTO redemptions (redemption_id, code, wallet_id, buyer_id, amount_cents, served_by)
VALUES (%(redemption_id)s, %(code)s, %(wallet_id)s, %(buyer_id)s, %(amount_cents)s, %(served_by)s)
"""


async def place_order_conditional_write(
    conn: Conn, *, drop_id: str, buyer_id: str, served_by: str
) -> OrderResult:
    """Claim one unit with a single conditional statement, decided on affected row count."""
    order_id = uuid4()
    async with conn.transaction():
        cursor = await conn.execute(SQL_CLAIM_UNIT_CONDITIONAL_WRITE, {"drop_id": drop_id})
        claimed = await cursor.fetchone()
        if claimed is None:
            # Zero rows affected: the unit was not claimed, and that decision is already final.
            # The read below happens only to classify a refusal that has already happened — it
            # cannot influence whether a unit is sold, so it introduces no check-then-act window.
            existence = await conn.execute(SQL_DROP_EXISTS, {"drop_id": drop_id})
            if await existence.fetchone() is None:
                return OrderResult(OrderOutcome.UNKNOWN_DROP)
            return OrderResult(OrderOutcome.REFUSED)
        await conn.execute(
            SQL_INSERT_ORDER,
            {
                "order_id": order_id,
                "drop_id": drop_id,
                "buyer_id": buyer_id,
                "served_by": served_by,
            },
        )
        return OrderResult(OrderOutcome.CONFIRMED, order_id, int(claimed["units_sold"]))


async def place_order_pessimistic_lock(
    conn: Conn, *, drop_id: str, buyer_id: str, served_by: str
) -> OrderResult:
    """Take the drop's row lock first, so the read-decide-write below is serialized."""
    order_id = uuid4()
    async with conn.transaction():
        cursor = await conn.execute(SQL_LOCK_DROP, {"drop_id": drop_id})
        locked = await cursor.fetchone()
        if locked is None:
            return OrderResult(OrderOutcome.UNKNOWN_DROP)
        units_available = int(locked["units_available"])
        units_sold = int(locked["units_sold"])
        # This read *is* decision-bearing — which is exactly why the row lock above must be held
        # for the whole transaction. Every other writer of this invariant blocks on it.
        if units_sold >= units_available:
            return OrderResult(OrderOutcome.REFUSED)
        await conn.execute(SQL_INCREMENT_UNITS_SOLD, {"drop_id": drop_id})
        await conn.execute(
            SQL_INSERT_ORDER,
            {
                "order_id": order_id,
                "drop_id": drop_id,
                "buyer_id": buyer_id,
                "served_by": served_by,
            },
        )
        return OrderResult(OrderOutcome.CONFIRMED, order_id, units_sold + 1)


async def redeem_credit_code(
    conn: Conn, *, code: str, wallet_id: str, buyer_id: str, served_by: str
) -> RedemptionResult:
    """Credit the wallet and record the redemption in one transaction; uniqueness decides."""
    redemption_id = uuid4()
    try:
        async with conn.transaction():
            cursor = await conn.execute(SQL_CREDIT_WALLET, {"wallet_id": wallet_id, "code": code})
            credited = await cursor.fetchone()
            if credited is None:
                raise _UnknownTargetError
            amount_cents = int(credited["amount_cents"])
            balance_cents = int(credited["balance_cents"])
            # If this insert violates redemptions_single_use, the credit applied a moment ago is
            # rolled back with it. A credit without its redemption record cannot survive.
            await conn.execute(
                SQL_INSERT_REDEMPTION,
                {
                    "redemption_id": redemption_id,
                    "code": code,
                    "wallet_id": wallet_id,
                    "buyer_id": buyer_id,
                    "amount_cents": amount_cents,
                    "served_by": served_by,
                },
            )
    except pg_errors.UniqueViolation:
        return RedemptionResult(RedemptionOutcome.REFUSED)
    except _UnknownTargetError:
        return RedemptionResult(RedemptionOutcome.UNKNOWN_TARGET)
    return RedemptionResult(RedemptionOutcome.CREDITED, redemption_id, amount_cents, balance_cents)


PlaceOrder = Callable[..., Awaitable[OrderResult]]

COUNTER_STRATEGIES: Final[dict[CounterGuard, PlaceOrder]] = {
    CounterGuard.CONDITIONAL_WRITE: place_order_conditional_write,
    CounterGuard.PESSIMISTIC_LOCK: place_order_pessimistic_lock,
}

__all__ = [
    "COUNTER_STRATEGIES",
    "place_order_conditional_write",
    "place_order_pessimistic_lock",
    "redeem_credit_code",
]
