"""The unguarded check-then-act shapes.

Read these next to `racejack/secure/guards.py`. The two files answer their callers identically; the
difference is entirely in the sequencing, and it is small enough to see at a glance:

* the secure counter guard is **one** statement whose `WHERE` clause carries the availability
  predicate, so the check *is* the write;
* the unguarded counter shape below is a `SELECT`, then a decision made against what that `SELECT`
  returned, then a **separate** `UPDATE`. Between the second and third of those there is an interval
  in which another request can do exactly the same thing. Everything the drop oversells, it
  oversells in that interval.

The single-use shape has the same structure with a different question: "has this already happened?"
instead of "how many are left?". It asks, is told no, and acts on an answer that was only ever a
claim about the past.

Neither shape is exotic and neither looks wrong. That is why this class of defect survives code
review, and why it survives a green test suite: exercised sequentially, both of these produce a
perfectly correct ledger.
"""

from __future__ import annotations

from typing import Final
from uuid import uuid4

from ..db import Conn
from ..instrumentation import EVENT_ACT, EVENT_CHECK, Instrumentation
from ..models import OrderOutcome, OrderResult, RedemptionOutcome, RedemptionResult

# --- the counter invariant, unguarded ----------------------------------------------------------

SQL_CHECK_COUNTER: Final = """
SELECT units_available, units_sold FROM drops WHERE drop_id = %(drop_id)s
"""

SQL_ACT_INCREMENT_UNITS_SOLD: Final = """
UPDATE drops SET units_sold = units_sold + 1 WHERE drop_id = %(drop_id)s RETURNING units_sold
"""

SQL_INSERT_ORDER: Final = """
INSERT INTO orders (order_id, drop_id, buyer_id, units, served_by)
VALUES (%(order_id)s, %(drop_id)s, %(buyer_id)s, 1, %(served_by)s)
"""

# --- the single-use invariant, unguarded -------------------------------------------------------

SQL_CHECK_ALREADY_REDEEMED: Final = """
SELECT count(*) AS redeemed FROM redemptions WHERE code = %(code)s
"""

SQL_READ_CODE_AND_WALLET: Final = """
SELECT c.amount_cents
  FROM credit_codes c, wallets w
 WHERE c.code = %(code)s AND w.wallet_id = %(wallet_id)s
"""

SQL_ACT_CREDIT_WALLET: Final = """
UPDATE wallets SET balance_cents = balance_cents + %(amount_cents)s
 WHERE wallet_id = %(wallet_id)s
RETURNING balance_cents
"""

SQL_INSERT_REDEMPTION: Final = """
INSERT INTO redemptions (redemption_id, code, wallet_id, buyer_id, amount_cents, served_by)
VALUES (%(redemption_id)s, %(code)s, %(wallet_id)s, %(buyer_id)s, %(amount_cents)s, %(served_by)s)
"""


async def place_order_unguarded(
    conn: Conn,
    instrumentation: Instrumentation,
    *,
    drop_id: str,
    buyer_id: str,
    served_by: str,
    request_id: str,
) -> OrderResult:
    """Check whether there is room, then claim a unit. Two steps, and a window between them."""
    # ---- TIME OF CHECK ------------------------------------------------------------------------
    cursor = await conn.execute(SQL_CHECK_COUNTER, {"drop_id": drop_id})
    drop = await cursor.fetchone()
    if drop is None:
        return OrderResult(OrderOutcome.UNKNOWN_DROP)
    units_available = int(drop["units_available"])
    units_sold = int(drop["units_sold"])
    await instrumentation.record(
        request_id=request_id, event=EVENT_CHECK, resource=drop_id, observed=units_sold
    )
    if units_sold >= units_available:
        return OrderResult(OrderOutcome.REFUSED)

    # ---- THE WINDOW ---------------------------------------------------------------------------
    # From here on, `units_sold` is a claim about the past. Nothing holds it true. In the natural
    # reproduction mode nothing at all happens on this line; in the deterministic mode the
    # instrumented synchronization point holds the window open so the interleaving is the same on
    # every machine. The window exists either way.
    await instrumentation.hold_window_open(request_id=request_id, observed=units_sold)

    # ---- TIME OF USE --------------------------------------------------------------------------
    order_id = uuid4()
    async with conn.transaction():
        written = await conn.execute(SQL_ACT_INCREMENT_UNITS_SOLD, {"drop_id": drop_id})
        row = await written.fetchone()
        await conn.execute(
            SQL_INSERT_ORDER,
            {
                "order_id": order_id,
                "drop_id": drop_id,
                "buyer_id": buyer_id,
                "served_by": served_by,
            },
        )
    now_sold = int(row["units_sold"]) if row else units_sold + 1
    await instrumentation.record(
        request_id=request_id,
        event=EVENT_ACT,
        resource=drop_id,
        observed=now_sold,
        detail=f"decided on units_sold={units_sold}",
    )
    return OrderResult(OrderOutcome.CONFIRMED, order_id, now_sold)


async def redeem_unguarded(
    conn: Conn,
    instrumentation: Instrumentation,
    *,
    code: str,
    wallet_id: str,
    buyer_id: str,
    served_by: str,
    request_id: str,
) -> RedemptionResult:
    """Check whether the code has been used, then use it. Two steps, and a window between them."""
    cursor = await conn.execute(SQL_READ_CODE_AND_WALLET, {"code": code, "wallet_id": wallet_id})
    target = await cursor.fetchone()
    if target is None:
        return RedemptionResult(RedemptionOutcome.UNKNOWN_TARGET)
    amount_cents = int(target["amount_cents"])

    # ---- TIME OF CHECK ------------------------------------------------------------------------
    counted = await conn.execute(SQL_CHECK_ALREADY_REDEEMED, {"code": code})
    row = await counted.fetchone()
    redeemed = int(row["redeemed"]) if row else 0
    await instrumentation.record(
        request_id=request_id, event=EVENT_CHECK, resource=code, observed=redeemed
    )
    if redeemed > 0:
        return RedemptionResult(RedemptionOutcome.REFUSED)

    # ---- THE WINDOW ---------------------------------------------------------------------------
    await instrumentation.hold_window_open(request_id=request_id, observed=redeemed)

    # ---- TIME OF USE --------------------------------------------------------------------------
    redemption_id = uuid4()
    async with conn.transaction():
        credited = await conn.execute(
            SQL_ACT_CREDIT_WALLET, {"amount_cents": amount_cents, "wallet_id": wallet_id}
        )
        balance_row = await credited.fetchone()
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
    balance_cents = int(balance_row["balance_cents"]) if balance_row else amount_cents
    await instrumentation.record(
        request_id=request_id,
        event=EVENT_ACT,
        resource=code,
        observed=balance_cents,
        detail=f"decided on redeemed={redeemed}",
    )
    return RedemptionResult(RedemptionOutcome.CREDITED, redemption_id, amount_cents, balance_cents)
