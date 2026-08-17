"""Client-visible payloads and internal outcome types.

These payloads are shared rather than per-variant on purpose. A later, deliberately vulnerable
application must return *identical* bodies for the same legitimate request, so that the only
difference between the two is how the check and the act are sequenced — never how they answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    CONFIRMED = "confirmed"


class RedemptionStatus(StrEnum):
    CREDITED = "credited"


class OrderResponse(BaseModel):
    order_id: UUID
    drop_id: str
    buyer_id: str
    units: int
    status: OrderStatus


class RedemptionRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    wallet_id: str = Field(min_length=1, max_length=128)


class RedemptionResponse(BaseModel):
    redemption_id: UUID
    code: str
    wallet_id: str
    buyer_id: str
    amount_cents: int
    wallet_balance_cents: int
    status: RedemptionStatus


class DropView(BaseModel):
    """The store's own view of a drop — enough to reconcile the ledger from outside."""

    drop_id: str
    product_name: str
    units_available: int
    units_sold: int
    units_remaining: int
    orders_confirmed: int


class WalletView(BaseModel):
    """The store's own view of a wallet — enough to reconcile credit from outside."""

    wallet_id: str
    owner_buyer_id: str
    balance_cents: int
    redemption_count: int
    total_credited_cents: int


class HealthResponse(BaseModel):
    status: str
    replica: str
    counter_guard: str


class OrderOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REFUSED = "refused"
    UNKNOWN_DROP = "unknown_drop"


class RedemptionOutcome(StrEnum):
    CREDITED = "credited"
    REFUSED = "refused"
    UNKNOWN_TARGET = "unknown_target"


@dataclass(frozen=True, slots=True)
class OrderResult:
    outcome: OrderOutcome
    order_id: UUID | None = None
    units_sold: int | None = None


@dataclass(frozen=True, slots=True)
class RedemptionResult:
    outcome: RedemptionOutcome
    redemption_id: UUID | None = None
    amount_cents: int | None = None
    wallet_balance_cents: int | None = None
