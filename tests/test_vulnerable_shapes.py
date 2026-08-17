"""The vulnerable application: the defect must actually happen, and only where it is meant to.

These tests drive intentionally broken educational material behind its opt-in controls. What they
assert is the mirror image of the secure suite: the deterministic mode is *required* to reproduce
the overrun, and a natural run is required not to be reported as a pass when it observes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from racejack import fixtures, schema
from racejack.config import HarnessConfig, RunnerConfig
from racejack.db import connect
from racejack.harness.burst import concurrent_burst
from racejack.harness.engine import Harness
from racejack.harness.ledger import ReproductionMode, Variant, Verdict
from racejack.httpclient import StorefrontHTTP
from racejack.instrumentation import GateSettings, arm_gate, disarm_gate, read_timeline
from racejack.seed import prepare_secure_run, prepare_vulnerable_run, present_backstops
from racejack.vulnerable.acknowledgement import (
    ACKNOWLEDGEMENT_VARIABLE,
    VulnerableDemoNotAcknowledgedError,
    require_acknowledgement,
)

ORDER_CONCURRENCY = 60
REDEMPTION_CONCURRENCY = 40


# --- the two opt-in actions --------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="absent"),
        pytest.param({"ALLOW_VULNERABLE_DEMO": ""}, id="empty"),
        pytest.param({"ALLOW_VULNERABLE_DEMO": "false"}, id="false"),
        pytest.param({"ALLOW_VULNERABLE_DEMO": "1"}, id="not-the-word-true"),
        pytest.param({"ALLOW_VULNERABLE_DEMO": "yes"}, id="yes-is-not-enough"),
    ],
)
def test_the_vulnerable_application_refuses_to_start_without_acknowledgement(
    env: dict[str, str],
) -> None:
    with pytest.raises(VulnerableDemoNotAcknowledgedError, match=ACKNOWLEDGEMENT_VARIABLE):
        require_acknowledgement(env)


def test_the_acknowledgement_is_accepted_when_given_exactly() -> None:
    require_acknowledgement({"ALLOW_VULNERABLE_DEMO": "true"})
    require_acknowledgement({"ALLOW_VULNERABLE_DEMO": " TRUE "})


# --- the backstop is removed for vulnerable runs, and only for those -----------------------------


async def test_a_vulnerable_run_removes_the_backstop_and_a_secure_run_restores_it(
    config: RunnerConfig,
) -> None:
    await prepare_vulnerable_run(config.database_url)
    async with connect(config.database_url) as conn:
        assert await present_backstops(conn) == set()

    await prepare_secure_run(config.database_url)
    async with connect(config.database_url) as conn:
        assert await present_backstops(conn) == set(schema.BACKSTOP_CONSTRAINTS)


# --- the shapes themselves ------------------------------------------------------------------------


@pytest.fixture
def vulnerable_config(
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


async def test_the_deterministic_mode_reproduces_both_overruns(
    vulnerable_config: HarnessConfig,
) -> None:
    report = await Harness(
        vulnerable_config, variant=Variant.VULNERABLE, mode=ReproductionMode.DETERMINISTIC
    ).run()
    failures = [check for check in report.checks if not check.passed]
    assert failures == [], "\n".join(f"{c.description}: {c.detail}" for c in failures)
    assert report.rounds_with_a_violation == len(report.rounds)
    assert all(
        result.reconciliation.verdict is Verdict.INVARIANT_VIOLATED for result in report.rounds
    )

    counter_rounds = [r for r in report.rounds if r.reconciliation.ledger.orders_issued]
    for result in counter_rounds:
        led = result.reconciliation.ledger
        assert led.orders_confirmed > led.units_available, "the drop was not oversold"
        assert led.units_remaining < 0, "the store did not report a negative remaining count"

    redemption_rounds = [r for r in report.rounds if r.reconciliation.ledger.redemptions_issued]
    for result in redemption_rounds:
        led = result.reconciliation.ledger
        assert led.redemptions > 1, "the single-use code was not redeemed more than once"
        assert led.wallet_balance_cents == led.redemptions * led.code_face_value_cents


async def test_the_timeline_shows_requests_reading_the_same_value_before_either_wrote(
    config: RunnerConfig, vulnerable_urls: tuple[str, ...], vulnerable_state: None
) -> None:
    concurrency = 8
    async with connect(config.database_url) as conn:
        await arm_gate(conn, GateSettings(expected=concurrency))
    async with StorefrontHTTP(
        vulnerable_urls, timeout=config.request_timeout_seconds, max_connections=concurrency
    ) as http:
        await http.wait_until_ready()
        await concurrent_burst(
            concurrency=concurrency,
            operation=lambda index: http.place_order(
                fixtures.DROP_ID, sequence=index, buyer_index=index, instrumented=True
            ),
        )
    async with connect(config.database_url) as conn:
        timeline = await read_timeline(conn)

    checks = [entry for entry in timeline if entry.event == "check"]
    acts = [entry for entry in timeline if entry.event == "act"]
    assert len(checks) == concurrency
    # Every check observed the same value, and every write came after every read. That is the race.
    assert len({entry.observed for entry in checks}) == 1
    assert max(entry.step for entry in checks) < min(entry.step for entry in acts)
    assert {entry.replica for entry in timeline} == {
        entry.replica for entry in timeline if entry.replica
    }


async def test_the_natural_mode_runs_no_instrumentation_at_all(
    config: RunnerConfig, vulnerable_urls: tuple[str, ...], vulnerable_state: None
) -> None:
    """Not "inert instrumentation" — none. No rendezvous, no recorded step, no wait."""
    async with connect(config.database_url) as conn:
        await disarm_gate(conn)
    async with StorefrontHTTP(
        vulnerable_urls, timeout=config.request_timeout_seconds, max_connections=16
    ) as http:
        await http.wait_until_ready()
        records = await concurrent_burst(
            concurrency=16,
            operation=lambda index: http.place_order(
                fixtures.DROP_ID, sequence=index, buyer_index=index
            ),
        )
    assert records
    async with connect(config.database_url) as conn:
        assert await read_timeline(conn) == [], "the natural mode recorded an instrumented step"


async def test_a_quiet_natural_round_is_never_reported_as_a_pass(
    vulnerable_config: HarnessConfig,
) -> None:
    report = await Harness(
        vulnerable_config, variant=Variant.VULNERABLE, mode=ReproductionMode.NATURAL
    ).run()
    # Whatever it observed, nothing here may be reported as the invariant holding.
    assert all(
        result.reconciliation.verdict is not Verdict.INVARIANT_HELD for result in report.rounds
    )
    assert report.passed, "a natural run must never fail merely for being inconclusive"


# --- the two variants answer identically ---------------------------------------------------------


async def test_a_legitimate_purchase_and_redemption_are_byte_identical_across_variants(
    config: RunnerConfig, vulnerable_urls: tuple[str, ...]
) -> None:
    """The variants differ in how check and act are sequenced — never in what they answer."""

    async def legitimate(urls: tuple[str, ...]) -> tuple[dict[str, object], dict[str, object]]:
        async with StorefrontHTTP(urls, timeout=config.request_timeout_seconds) as http:
            await http.wait_until_ready()
            order = await http.place_order(fixtures.DROP_ID, sequence=1, buyer_index=1)
            redemption = await http.redeem(
                sequence=2,
                code=fixtures.CREDIT_CODE,
                wallet_id=fixtures.WALLET_ID,
                buyer_index=1,
            )
        assert order.body is not None
        assert redemption.body is not None
        return order.body, redemption.body

    await prepare_secure_run(config.database_url)
    secure_order, secure_redemption = await legitimate(config.replica_urls)
    await prepare_vulnerable_run(config.database_url)
    vulnerable_order, vulnerable_redemption = await legitimate(vulnerable_urls)
    await prepare_secure_run(config.database_url)

    # The identifiers are generated per request; everything else must match exactly.
    assert secure_order.keys() == vulnerable_order.keys()
    assert secure_redemption.keys() == vulnerable_redemption.keys()
    for key in secure_order:
        if key == "order_id":
            continue
        assert secure_order[key] == vulnerable_order[key], f"order payload differs at {key!r}"
    for key in secure_redemption:
        if key == "redemption_id":
            continue
        assert secure_redemption[key] == vulnerable_redemption[key], (
            f"redemption payload differs at {key!r}"
        )
