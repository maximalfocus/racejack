"""Reconciliation, canonical state, and — the important one — what zero violations means.

A quiet run means opposite things depending on what was being driven, and getting that wrong is the
exact reasoning error the demonstration exists to correct. So it is asserted here rather than left
to a comment.
"""

from __future__ import annotations

import json

import pytest

from racejack.harness.ledger import (
    Invariant,
    Ledger,
    Reconciliation,
    ReproductionMode,
    Variant,
    Verdict,
    ledger_from_views,
)

UNITS = 12
FACE_VALUE = 2500


def make_ledger(
    *,
    orders_confirmed: int = UNITS,
    units_sold: int | None = None,
    redemptions: int = 0,
    credited: int | None = None,
    balance: int | None = None,
) -> Ledger:
    units_sold = orders_confirmed if units_sold is None else units_sold
    credited = redemptions * FACE_VALUE if credited is None else credited
    balance = credited if balance is None else balance
    return Ledger(
        units_available=UNITS,
        units_sold=units_sold,
        units_remaining=UNITS - units_sold,
        orders_confirmed=orders_confirmed,
        orders_issued=60,
        orders_refused=60 - orders_confirmed,
        code_face_value_cents=FACE_VALUE,
        redemptions=redemptions,
        redemptions_issued=0,
        redemptions_refused=0,
        total_credited_cents=credited,
        wallet_balance_cents=balance,
    )


def reconcile(
    ledger: Ledger,
    *,
    invariant: Invariant = Invariant.COUNTER,
    variant: Variant = Variant.SECURE,
) -> Reconciliation:
    return Reconciliation(ledger, invariant, variant, ReproductionMode.NATURAL)


def test_a_held_counter_invariant_reports_no_overrun_and_no_shortfall() -> None:
    result = reconcile(make_ledger())
    assert result.overrun == 0
    assert result.shortfall == 0
    assert result.violations == 0
    assert result.verdict is Verdict.INVARIANT_HELD


def test_an_oversold_drop_is_a_violation() -> None:
    result = reconcile(make_ledger(orders_confirmed=60, units_sold=60))
    assert result.overrun == 48
    assert result.violations >= 48
    assert result.verdict is Verdict.INVARIANT_VIOLATED


def test_selling_fewer_than_it_owns_is_a_shortfall_not_a_violation() -> None:
    """Protecting the invariant by refusing valid requests is a different failure, and still one."""
    result = reconcile(make_ledger(orders_confirmed=11, units_sold=11))
    assert result.shortfall == 1
    assert result.overrun == 0
    assert result.violations == 0
    assert result.verdict is Verdict.INVARIANT_HELD


def test_a_second_redemption_is_a_violation() -> None:
    ledger = make_ledger(orders_confirmed=0, redemptions=40)
    result = reconcile(ledger, invariant=Invariant.SINGLE_USE)
    assert result.overrun == 39
    assert result.verdict is Verdict.INVARIANT_VIOLATED


def test_a_credit_without_its_redemption_record_is_a_violation() -> None:
    ledger = make_ledger(orders_confirmed=0, redemptions=1, credited=FACE_VALUE, balance=5000)
    result = reconcile(ledger, invariant=Invariant.SINGLE_USE)
    assert result.partial_redemptions == 2500
    assert result.verdict is Verdict.INVARIANT_VIOLATED


def test_the_stores_counter_disagreeing_with_its_own_orders_is_a_violation() -> None:
    result = reconcile(make_ledger(orders_confirmed=12, units_sold=9))
    assert result.ledger_disagreement == 3
    assert result.verdict is Verdict.INVARIANT_VIOLATED


def test_zero_violations_against_the_secure_application_is_a_pass() -> None:
    result = reconcile(make_ledger(), variant=Variant.SECURE)
    assert result.verdict is Verdict.INVARIANT_HELD


def test_zero_violations_against_a_vulnerable_application_is_inconclusive() -> None:
    """Never a pass, and never evidence the code is correct — that inference is the whole error."""
    quiet_run = reconcile(make_ledger(), variant=Variant.VULNERABLE)
    identical_ledger_secure_side = reconcile(make_ledger(), variant=Variant.SECURE)
    # Byte-identical ledgers, opposite conclusions. What differs is only what was being driven.
    assert quiet_run.ledger == identical_ledger_secure_side.ledger
    assert quiet_run.verdict is Verdict.INCONCLUSIVE
    assert identical_ledger_secure_side.verdict is Verdict.INVARIANT_HELD


def test_a_vulnerable_run_that_does_lose_a_race_is_still_a_violation() -> None:
    result = reconcile(make_ledger(orders_confirmed=13, units_sold=13), variant=Variant.VULNERABLE)
    assert result.verdict is Verdict.INVARIANT_VIOLATED


def test_canonical_state_ignores_what_is_legitimately_run_dependent() -> None:
    concurrent = make_ledger()
    sequential = Ledger(
        **{
            **{field: getattr(concurrent, field) for field in Ledger.__slots__},
            # A different number of requests lost the race; the resulting state is the same.
            "orders_issued": 1,
            "orders_refused": 0,
        }
    )
    assert concurrent.canonical_state() == sequential.canonical_state()


def test_canonical_state_does_not_ignore_the_invariant_bearing_state() -> None:
    assert make_ledger().canonical_state() != make_ledger(orders_confirmed=11).canonical_state()


def test_canonical_state_is_stable_and_sorted() -> None:
    parsed = json.loads(make_ledger().canonical_state())
    assert set(parsed) == {"drop", "wallet"}
    assert make_ledger().canonical_state() == make_ledger().canonical_state()


def test_ledger_is_built_from_the_stores_own_views() -> None:
    ledger = ledger_from_views(
        drop={
            "units_available": 12,
            "units_sold": 12,
            "units_remaining": 0,
            "orders_confirmed": 12,
        },
        wallet={"balance_cents": 2500, "redemption_count": 1, "total_credited_cents": 2500},
        code_face_value_cents=FACE_VALUE,
        orders_issued=60,
        orders_refused=48,
        redemptions_issued=40,
        redemptions_refused=39,
    )
    assert ledger.orders_confirmed == 12
    assert ledger.redemptions == 1
    assert reconcile(ledger).violations == 0


@pytest.mark.parametrize("invariant", list(Invariant))
def test_every_reconciliation_renders_lines_a_human_can_read(invariant: Invariant) -> None:
    lines = reconcile(make_ledger(redemptions=1), invariant=invariant).as_lines()
    assert any("units available" in line for line in lines)
    assert any("code face value" in line for line in lines)
    assert any("VERDICT" in line for line in lines)
