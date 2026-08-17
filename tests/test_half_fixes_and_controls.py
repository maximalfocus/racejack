"""The two shapes that look like fixes, and the two controls that mark what a race is not.

One live pass of the deterministic ladder produces every fact this module needs, so it runs the
engine once and then asserts against the rounds. What it is checking is deliberately *not* uniform:
the lock is required to hold at one replica and to break at two; the transaction is required to
change nothing; the sequential run is required to be perfectly correct; and the throttled runs are
required to do less damage without ever doing none.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from racejack import fixtures
from racejack.config import HarnessConfig, RunnerConfig, VulnerableShape
from racejack.harness.burst import concurrent_burst
from racejack.harness.engine import (
    APPLIED_THROTTLE_STEPS,
    Expectation,
    Harness,
    HarnessReport,
    RoundResult,
)
from racejack.harness.ledger import Invariant, ReproductionMode, Variant, Verdict
from racejack.httpclient import RequestRecord

ORDER_CONCURRENCY = 60
REDEMPTION_CONCURRENCY = 40


@pytest.fixture
def ladder_config(
    config: RunnerConfig, vulnerable_urls: tuple[str, ...], tmp_path: Path
) -> HarnessConfig:
    assert vulnerable_urls
    return HarnessConfig(
        runner=config,
        order_concurrency=ORDER_CONCURRENCY,
        redemption_concurrency=REDEMPTION_CONCURRENCY,
        rounds=1,
        transcript_path=tmp_path / "transcript.txt",
    )


@pytest.fixture
async def ladder(ladder_config: HarnessConfig) -> HarnessReport:
    return await Harness(
        ladder_config, variant=Variant.VULNERABLE, mode=ReproductionMode.DETERMINISTIC
    ).run()


def _rounds(report: HarnessReport, **match: object) -> list[RoundResult]:
    return [
        result
        for result in report.rounds
        if all(getattr(result.scenario, key) == value for key, value in match.items())
    ]


def test_the_whole_deterministic_ladder_is_green(ladder: HarnessReport) -> None:
    failures = [check for check in ladder.checks if not check.passed]
    assert failures == [], "\n".join(f"{c.description}: {c.detail}" for c in failures)


# --- half-fix 1: the process-scoped lock -------------------------------------------------------


def test_the_process_lock_holds_behind_one_replica(ladder: HarnessReport) -> None:
    at_one = _rounds(ladder, shape=VulnerableShape.PROCESS_LOCK.value, replicas=1)
    assert at_one, "the process-scoped lock never ran at one replica"
    for result in at_one:
        led = result.reconciliation.ledger
        assert result.reconciliation.overrun == 0, "the lock did not hold at one replica"
        assert led.orders_confirmed == led.units_available
        assert led.units_remaining == 0


def test_the_process_lock_breaks_at_two_replicas(ladder: HarnessReport) -> None:
    at_two = _rounds(ladder, shape=VulnerableShape.PROCESS_LOCK.value, replicas=2)
    assert at_two, "the process-scoped lock never ran at two replicas"
    for result in at_two:
        led = result.reconciliation.ledger
        assert result.reconciliation.overrun > 0, "the lock did not break at two replicas"
        assert led.orders_confirmed > led.units_available
        assert led.units_remaining < 0


def test_nothing_but_the_replica_count_differs_between_the_two_lock_runs(
    ladder: HarnessReport,
) -> None:
    """If anything else differed, the comparison would prove nothing."""
    at_one = _rounds(ladder, shape=VulnerableShape.PROCESS_LOCK.value, replicas=1)[0].scenario
    at_two = _rounds(ladder, shape=VulnerableShape.PROCESS_LOCK.value, replicas=2)[0].scenario
    assert at_one.invariant == at_two.invariant
    assert at_one.shape == at_two.shape == VulnerableShape.PROCESS_LOCK.value
    assert at_one.shape_header == at_two.shape_header
    assert at_one.concurrency == at_two.concurrency
    assert at_one.units_pre_sold == at_two.units_pre_sold == fixtures.DROP_UNITS_AVAILABLE - 1
    assert at_one.stagger_seconds == at_two.stagger_seconds
    assert at_one.wave_size == at_two.wave_size
    # The gate width follows from the replica count — one request per process can be at the check
    # at once — which is the mechanism itself, not a second independent variable.
    assert at_one.gate is not None
    assert at_two.gate is not None
    assert at_one.gate.arm_at == at_two.gate.arm_at
    assert (at_one.gate.expected, at_two.gate.expected) == (1, 2)


# --- half-fix 2: the single transaction --------------------------------------------------------


def test_one_transaction_at_read_committed_still_loses_updates(ladder: HarnessReport) -> None:
    rounds = _rounds(ladder, shape=VulnerableShape.SINGLE_TRANSACTION.value)
    assert rounds, "the single-transaction shape never ran"
    for result in rounds:
        led = result.reconciliation.ledger
        assert result.reconciliation.overrun > 0, "wrapping it in a transaction fixed it"
        assert led.orders_confirmed > led.units_available
        assert led.units_remaining < 0


def test_the_isolation_level_in_effect_is_reported(ladder: HarnessReport) -> None:
    rounds = _rounds(ladder, shape=VulnerableShape.SINGLE_TRANSACTION.value)
    for result in rounds:
        assert result.isolation_levels, "the isolation level was not reported"
        for level in result.isolation_levels:
            assert "serializable" not in level.lower()


def test_the_transaction_shape_is_no_better_than_no_transaction_at_all(
    ladder: HarnessReport,
) -> None:
    """Atomicity is not isolation, and the ledger says so."""
    unguarded = _rounds(
        ladder, shape=VulnerableShape.UNGUARDED.value, invariant=Invariant.COUNTER, replicas=2
    )
    transactional = _rounds(ladder, shape=VulnerableShape.SINGLE_TRANSACTION.value, replicas=2)
    assert unguarded and transactional
    assert transactional[0].reconciliation.overrun == unguarded[0].reconciliation.overrun, (
        "the transaction changed the outcome, which would be a different lesson"
    )


# --- control 1: sequential execution passes ----------------------------------------------------


def test_the_identical_vulnerable_code_is_exactly_correct_run_sequentially(
    ladder: HarnessReport,
) -> None:
    rounds = _rounds(ladder, sequential=True)
    assert len(rounds) >= 2, "the sequential control did not cover both invariants"
    for result in rounds:
        led = result.reconciliation.ledger
        assert result.reconciliation.overrun == 0
        assert result.reconciliation.shortfall == 0
        assert result.reconciliation.ledger_disagreement == 0
        assert result.reconciliation.partial_redemptions == 0
        if result.scenario.invariant is Invariant.COUNTER:
            assert led.orders_confirmed == fixtures.DROP_UNITS_AVAILABLE
        else:
            assert led.redemptions == 1
            assert led.wallet_balance_cents == fixtures.CREDIT_CODE_AMOUNT_CENTS


def test_a_clean_sequential_run_of_vulnerable_code_is_still_not_a_pass(
    ladder: HarnessReport,
) -> None:
    """This is the whole reason the defect ships, so the verdict must not read as reassurance."""
    for result in _rounds(ladder, sequential=True):
        assert result.reconciliation.verdict is Verdict.INCONCLUSIVE, (
            "a quiet run of vulnerable code must never be labelled as the invariant holding"
        )


# --- control 2: throttling narrows the window without closing it -------------------------------


def test_throttling_reduces_the_damage_without_ever_removing_it(ladder: HarnessReport) -> None:
    throttled = [
        result
        for result in ladder.rounds
        if result.scenario.invariant is Invariant.SINGLE_USE
        and result.scenario.shape == VulnerableShape.UNGUARDED.value
        and not result.scenario.sequential
        and result.scenario.replicas == 2
        and result.scenario.gate is not None
    ]
    by_width = {
        result.scenario.gate.expected: result.reconciliation.overrun
        for result in throttled
        if result.scenario.gate is not None
    }
    assert len(by_width) >= 3, f"expected at least three throttle settings, got {by_width}"
    widths = sorted(by_width, reverse=True)
    overruns = [by_width[width] for width in widths]
    assert overruns == sorted(overruns, reverse=True), f"the rate did not fall: {by_width}"
    assert len(set(overruns)) == len(overruns), f"the rate did not strictly fall: {by_width}"
    assert all(overrun > 0 for overrun in overruns), (
        f"throttling reached zero, which would make it a fix: {by_width}"
    )
    # Every request that got inside the window redeemed a single-use code exactly once too many.
    for width, overrun in by_width.items():
        assert overrun == width - 1, f"window {width} produced overrun {overrun}"


def test_the_applied_throttle_settings_are_real_and_distinct() -> None:
    assert len(set(APPLIED_THROTTLE_STEPS)) == len(APPLIED_THROTTLE_STEPS)
    assert len(APPLIED_THROTTLE_STEPS) >= 2, "at least two throttle settings are required"
    assert all(step > 0 for step in APPLIED_THROTTLE_STEPS), (
        "the unthrottled baseline is the plain scenario; these must be actual throttles"
    )


# --- the client-side throttle itself -----------------------------------------------------------


def _record(index: int) -> RequestRecord:
    return RequestRecord(index, "order", f"buyer-{index:04d}", "vuln-a", "vuln-a", 201, "r", None)


async def test_a_wave_limits_how_many_requests_are_in_flight_at_once() -> None:
    in_flight = 0
    peak = 0

    async def operation(index: int) -> RequestRecord:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _record(index)

    records = await concurrent_burst(concurrency=40, operation=operation, wave_size=8)
    assert len(records) == 40
    assert peak == 8, f"a wave of 8 put {peak} requests in flight"
    assert [record.sequence for record in records] == list(range(1, 41))


async def test_no_wave_size_means_the_whole_burst_at_once() -> None:
    in_flight = 0
    peak = 0

    async def operation(index: int) -> RequestRecord:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _record(index)

    await concurrent_burst(concurrency=16, operation=operation)
    assert peak == 16


@pytest.mark.parametrize("bad", [0, -3])
async def test_a_meaningless_wave_size_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="wave_size"):
        await concurrent_burst(concurrency=8, operation=lambda i: _noop(i), wave_size=bad)


async def _noop(index: int) -> RequestRecord:
    return _record(index)


# --- the matrix covers what it claims to -------------------------------------------------------


def test_the_deterministic_ladder_covers_every_shape_and_every_expectation(
    ladder: HarnessReport,
) -> None:
    shapes = {result.scenario.shape for result in ladder.rounds}
    assert shapes == {shape.value for shape in VulnerableShape}
    expectations = {result.scenario.expectation for result in ladder.rounds}
    assert expectations == {
        Expectation.DEFECT_REQUIRED,
        Expectation.LOOKS_FIXED,
        Expectation.SEQUENTIAL_CORRECT,
    }
    assert {result.scenario.invariant for result in ladder.rounds} == set(Invariant)
