"""Deterministic, wholly fictional fixtures.

Every identifier, name, product, promotional code, wallet balance, and token in this module is
invented for the demonstration. Nothing here refers to a real store, person, payment system, or
credential. Values are stable across runs and machines so that two runs of the same scenario are
comparable, and every run recreates them from scratch so no run inherits another run's state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

STORE_NAME: Final = "Kestrel Supply"

DROP_ID: Final = "DROP-2026-03"
DROP_PRODUCT_NAME: Final = "Tidewater Field Jacket"
DROP_UNITS_AVAILABLE: Final = 12

CREDIT_CODE: Final = "KESTREL-WELCOME-2500"
CREDIT_CODE_AMOUNT_CENTS: Final = 2500

WALLET_ID: Final = "wallet-kestrel-demo"

BUYER_COUNT: Final = 96
"""Enough fictional buyers for the largest concurrent burst the demonstration ever issues."""

EXPIRED_BUYER_ID: Final = "buyer-expired"

# Invented names, cycled deterministically so buyer-0042 is the same fictional person every run.
_GIVEN_NAMES: Final = (
    "Avery", "Brook", "Casey", "Devin", "Emery", "Finley", "Gray", "Harper",
    "Indigo", "Jules", "Kai", "Lennox", "Marlow", "Nova", "Oakley", "Peyton",
)  # fmt: skip
_FAMILY_NAMES: Final = (
    "Ashcroft", "Barrowman", "Calderwood", "Dunmore", "Ellsworth", "Fairbanks",
)  # fmt: skip


def buyer_id(index: int) -> str:
    """The fictional buyer identifier for a one-based index."""
    return f"buyer-{index:04d}"


def display_name(index: int) -> str:
    """A conspicuously fictional display name, derived deterministically from the index."""
    given = _GIVEN_NAMES[(index - 1) % len(_GIVEN_NAMES)]
    family = _FAMILY_NAMES[((index - 1) // len(_GIVEN_NAMES)) % len(_FAMILY_NAMES)]
    return f"{given} {family}"


@dataclass(frozen=True, slots=True)
class Buyer:
    buyer_id: str
    display_name: str


BUYERS: Final[tuple[Buyer, ...]] = (
    *(Buyer(buyer_id(i), display_name(i)) for i in range(1, BUYER_COUNT + 1)),
    Buyer(EXPIRED_BUYER_ID, "Quinn Everhart (expired demo session)"),
)

WALLET_OWNER_BUYER_ID: Final = BUYERS[0].buyer_id
