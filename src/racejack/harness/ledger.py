"""Ledger reconciliation, canonical state, and the invariant verdict.

The reconciliation exists so a violated invariant is impossible to miss. It puts the two numbers
that must agree next to each other — what the store owned against what it sold, what a code was
worth against what was credited — and then states a verdict in words.

One distinction in here is the whole reason the project exists. Zero observed violations means
different things depending on what you were driving:

* against the **secure** application it is an exact assertion and a pass — the invariant held, and a
  secure-side violation would be a genuine failure rather than a flake to retry away;
* against a **vulnerable** application under the natural reproduction mode it is
  **`inconclusive`** — never a pass, and never evidence the code is correct. Concluding otherwise
  from a quiet run is precisely the reasoning error this demonstration was built to correct.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Variant(StrEnum):
    """Which application the harness was driving."""

    SECURE = "secure"
    VULNERABLE = "vulnerable"
    """No such application exists yet; the verdict semantics for it do."""


class ReproductionMode(StrEnum):
    NATURAL = "natural"
    """Genuine concurrent load with no instrumentation whatsoever in any code path."""

    DETERMINISTIC = "deterministic"
    """An instrumented synchronization point holds the window open, identically on every machine.

    Vulnerable paths only. The window is a genuine property of check-then-act code; this only holds
    it open long enough to observe, and the natural mode is the evidence that it exists without any
    help. This is the mode that carries the required assertion that the defect happens.
    """


class Invariant(StrEnum):
    COUNTER = "counter"
    """units_sold must never exceed units_available."""

    SINGLE_USE = "single-use"
    """each credit code must be redeemed exactly once."""


class Verdict(StrEnum):
    INVARIANT_HELD = "invariant held"
    INVARIANT_VIOLATED = "invariant violated"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class Ledger:
    """What the store says about itself after a round, plus what the harness sent it."""

    units_available: int
    units_sold: int
    units_remaining: int
    orders_confirmed: int
    orders_issued: int
    orders_refused: int
    code_face_value_cents: int
    redemptions: int
    redemptions_issued: int
    redemptions_refused: int
    total_credited_cents: int
    wallet_balance_cents: int

    def canonical_state(self) -> str:
        """The invariant-bearing state, normalized so two correct runs are byte-for-byte equal.

        Deliberately excludes everything that is legitimately run-dependent — order and redemption
        identifiers, timestamps, and *which* particular buyers won a race. What remains is exactly
        what the store reports about itself, and a correct concurrent run must produce the same
        bytes here as a correct sequential one.
        """
        return json.dumps(
            {
                "drop": {
                    "units_available": self.units_available,
                    "units_sold": self.units_sold,
                    "units_remaining": self.units_remaining,
                    "orders_confirmed": self.orders_confirmed,
                },
                "wallet": {
                    "balance_cents": self.wallet_balance_cents,
                    "redemption_count": self.redemptions,
                    "total_credited_cents": self.total_credited_cents,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The ledger, judged against the invariant the round was exercising."""

    ledger: Ledger
    invariant: Invariant
    variant: Variant
    mode: ReproductionMode

    @property
    def overrun(self) -> int:
        """How far past the invariant the store went. Anything above zero is the defect."""
        if self.invariant is Invariant.COUNTER:
            return max(0, self.ledger.orders_confirmed - self.ledger.units_available)
        return max(0, self.ledger.redemptions - 1)

    @property
    def shortfall(self) -> int:
        """Legitimate work the store refused. Not a safety violation — a different failure."""
        if self.invariant is Invariant.COUNTER:
            return max(0, self.ledger.units_available - self.ledger.orders_confirmed)
        return max(0, 1 - self.ledger.redemptions)

    @property
    def partial_redemptions(self) -> int:
        """A credit without its redemption record, or the reverse. Must be impossible."""
        return abs(self.ledger.wallet_balance_cents - self.ledger.total_credited_cents)

    @property
    def ledger_disagreement(self) -> int:
        """The store's own counter against its own order records; disagreement is itself a
        finding."""
        return abs(self.ledger.units_sold - self.ledger.orders_confirmed)

    @property
    def violations(self) -> int:
        return self.overrun + self.partial_redemptions + self.ledger_disagreement

    @property
    def verdict(self) -> Verdict:
        if self.violations > 0:
            return Verdict.INVARIANT_VIOLATED
        if self.variant is Variant.SECURE:
            return Verdict.INVARIANT_HELD
        # A vulnerable variant that happened not to lose a race this time proves nothing at all.
        return Verdict.INCONCLUSIVE

    def summary(self) -> str:
        """The ledger numbers on one line, for a failure detail."""
        return " | ".join(self.as_lines()[:2])

    def as_lines(self) -> list[str]:
        """The reconciliation as a human reads it: the numbers that must agree, side by side."""
        led = self.ledger
        return [
            f"units available {led.units_available:>6}   "
            f"units sold {led.units_sold:>6}   "
            f"units remaining {led.units_remaining:>6}",
            f"orders confirmed {led.orders_confirmed:>5}   "
            f"overrun {self.overrun:>9}   shortfall {self.shortfall:>7}",
            f"code face value {led.code_face_value_cents:>6}   "
            f"redemptions      {led.redemptions:>6}   "
            f"credited {led.total_credited_cents:>6}   wallet    "
            f"{led.wallet_balance_cents:>6}",
            f"requests issued  orders {led.orders_issued:>5} "
            f"(refused {led.orders_refused})   "
            f"redemptions {led.redemptions_issued:>5} (refused {led.redemptions_refused})",
            f"VERDICT: {self.verdict.value.upper()}   "
            f"({self.variant.value} application, {self.mode.value} reproduction mode)",
        ]


def ledger_from_views(
    *,
    drop: dict[str, Any],
    wallet: dict[str, Any],
    code_face_value_cents: int,
    orders_issued: int,
    orders_refused: int,
    redemptions_issued: int,
    redemptions_refused: int,
) -> Ledger:
    """Build a ledger from the store's own two views — never from database inspection."""
    return Ledger(
        units_available=int(drop["units_available"]),
        units_sold=int(drop["units_sold"]),
        units_remaining=int(drop["units_remaining"]),
        orders_confirmed=int(drop["orders_confirmed"]),
        orders_issued=orders_issued,
        orders_refused=orders_refused,
        code_face_value_cents=code_face_value_cents,
        redemptions=int(wallet["redemption_count"]),
        redemptions_issued=redemptions_issued,
        redemptions_refused=redemptions_refused,
        total_credited_cents=int(wallet["total_credited_cents"]),
        wallet_balance_cents=int(wallet["balance_cents"]),
    )
