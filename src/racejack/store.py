"""The store's own read-side view of its state.

Every outcome the demonstration claims is established from the application's own responses rather
than from direct database inspection, so these two views must expose enough for a reader to
reconcile the ledger: how many units the store believes it sold *and* how many order
records it actually holds; how much credit the wallet believes it has *and* how many redemptions
produced it. When those disagree, the disagreement is the finding.
"""

from __future__ import annotations

from typing import Final

from .db import Conn
from .models import DropView, WalletView

SQL_DROP_VIEW: Final = """
SELECT d.drop_id,
       d.product_name,
       d.units_available,
       d.units_sold,
       (SELECT count(*) FROM orders o WHERE o.drop_id = d.drop_id) AS orders_confirmed
  FROM drops d
 WHERE d.drop_id = %(drop_id)s
"""

SQL_WALLET_VIEW: Final = """
SELECT w.wallet_id,
       w.owner_buyer_id,
       w.balance_cents,
       (SELECT count(*) FROM redemptions r WHERE r.wallet_id = w.wallet_id) AS redemption_count,
       (SELECT coalesce(sum(r.amount_cents), 0) FROM redemptions r WHERE r.wallet_id = w.wallet_id)
           AS total_credited_cents
  FROM wallets w
 WHERE w.wallet_id = %(wallet_id)s
"""


async def read_drop(conn: Conn, drop_id: str) -> DropView | None:
    cursor = await conn.execute(SQL_DROP_VIEW, {"drop_id": drop_id})
    row = await cursor.fetchone()
    if row is None:
        return None
    units_available = int(row["units_available"])
    units_sold = int(row["units_sold"])
    return DropView(
        drop_id=str(row["drop_id"]),
        product_name=str(row["product_name"]),
        units_available=units_available,
        units_sold=units_sold,
        # Reported, not clamped. A store that has sold more than it owns should say so.
        units_remaining=units_available - units_sold,
        orders_confirmed=int(row["orders_confirmed"]),
    )


async def read_wallet(conn: Conn, wallet_id: str) -> WalletView | None:
    cursor = await conn.execute(SQL_WALLET_VIEW, {"wallet_id": wallet_id})
    row = await cursor.fetchone()
    if row is None:
        return None
    return WalletView(
        wallet_id=str(row["wallet_id"]),
        owner_buyer_id=str(row["owner_buyer_id"]),
        balance_cents=int(row["balance_cents"]),
        redemption_count=int(row["redemption_count"]),
        total_credited_cents=int(row["total_credited_cents"]),
    )
