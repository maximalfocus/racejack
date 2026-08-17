"""The harness against the running store: exact outcomes under genuine concurrent load.

These are the assertions a sequential test cannot make. Sixty buyers arrive at a twelve-unit drop at
the same moment; forty redemptions of one single-use code arrive at the same moment. What comes back
must be exactly twelve orders — *not fewer* — and exactly one credit, every time, at one replica and
at two, with the store's own ledger reconciling and its canonical state matching a correct
sequential run byte for byte.
"""

from __future__ import annotations

import pytest

from racejack import fixtures
from racejack.config import CounterGuard, HarnessConfig, RunnerConfig
from racejack.harness.burst import concurrent_burst, sequential_run
from racejack.harness.engine import Harness
from racejack.harness.ledger import Verdict
from racejack.httpclient import StorefrontHTTP
from racejack.seed import seed

ORDER_CONCURRENCY = 60
REDEMPTION_CONCURRENCY = 40

GUARDS = [pytest.param(guard, id=guard.value) for guard in CounterGuard]


@pytest.fixture
def harness_config(config: RunnerConfig, tmp_path: object) -> HarnessConfig:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    return HarnessConfig(
        runner=config,
        order_concurrency=ORDER_CONCURRENCY,
        redemption_concurrency=REDEMPTION_CONCURRENCY,
        rounds=1,
        transcript_path=tmp_path / "harness-transcript.txt",
    )


@pytest.mark.parametrize("guard", GUARDS)
async def test_sixty_concurrent_buyers_get_exactly_twelve_orders(
    config: RunnerConfig, fresh_state: None, guard: CounterGuard
) -> None:
    async with StorefrontHTTP(
        config.replica_urls,
        timeout=config.request_timeout_seconds,
        max_connections=ORDER_CONCURRENCY,
    ) as http:
        await http.wait_until_ready()
        labels = set(http.replica_labels)
        records = await concurrent_burst(
            concurrency=ORDER_CONCURRENCY,
            operation=lambda index: http.place_order(
                fixtures.DROP_ID, sequence=index, buyer_index=index, guard=guard.value
            ),
        )
        view = await http.read_drop(fixtures.DROP_ID, sequence=ORDER_CONCURRENCY + 1)

    confirmed = [record for record in records if record.succeeded]
    refused = [record for record in records if record.refused]
    assert len(confirmed) == fixtures.DROP_UNITS_AVAILABLE, "not exactly the units it owned"
    assert len(refused) == ORDER_CONCURRENCY - fixtures.DROP_UNITS_AVAILABLE
    assert len(confirmed) + len(refused) == ORDER_CONCURRENCY, "a request got neither answer"
    assert {record.served_by for record in records} == labels, (
        "the burst did not reach every replica"
    )

    assert view.body is not None
    assert view.body["units_sold"] == fixtures.DROP_UNITS_AVAILABLE
    assert view.body["units_remaining"] == 0
    assert view.body["orders_confirmed"] == fixtures.DROP_UNITS_AVAILABLE


async def test_forty_concurrent_redemptions_credit_the_code_exactly_once(
    config: RunnerConfig, fresh_state: None
) -> None:
    async with StorefrontHTTP(
        config.replica_urls,
        timeout=config.request_timeout_seconds,
        max_connections=REDEMPTION_CONCURRENCY,
    ) as http:
        await http.wait_until_ready()
        records = await concurrent_burst(
            concurrency=REDEMPTION_CONCURRENCY,
            operation=lambda index: http.redeem(
                sequence=index,
                code=fixtures.CREDIT_CODE,
                wallet_id=fixtures.WALLET_ID,
                buyer_index=index,
            ),
        )
        view = await http.read_wallet(fixtures.WALLET_ID, sequence=REDEMPTION_CONCURRENCY + 1)

    credited = [record for record in records if record.succeeded]
    assert len(credited) == 1
    assert view.body is not None
    assert view.body["redemption_count"] == 1
    assert view.body["balance_cents"] == fixtures.CREDIT_CODE_AMOUNT_CENTS
    # No partially applied redemption: the credit and its record share one transaction.
    assert view.body["balance_cents"] == view.body["total_credited_cents"]


async def test_a_concurrent_run_leaves_the_same_state_as_a_sequential_one(
    config: RunnerConfig, fresh_state: None
) -> None:
    async def drive(concurrent: bool) -> dict[str, object]:
        await seed(config.database_url, create=False)
        async with StorefrontHTTP(
            config.replica_urls,
            timeout=config.request_timeout_seconds,
            max_connections=ORDER_CONCURRENCY,
        ) as http:
            await http.wait_until_ready()
            operation = lambda index: http.place_order(  # noqa: E731
                fixtures.DROP_ID, sequence=index, buyer_index=index
            )
            if concurrent:
                await concurrent_burst(concurrency=ORDER_CONCURRENCY, operation=operation)
            else:
                await sequential_run(count=ORDER_CONCURRENCY, operation=operation)
            view = await http.read_drop(fixtures.DROP_ID, sequence=ORDER_CONCURRENCY + 1)
        assert view.body is not None
        return view.body

    assert await drive(concurrent=True) == await drive(concurrent=False)


async def test_the_engine_reports_every_scenario_green(
    harness_config: HarnessConfig, fresh_state: None
) -> None:
    """One full pass of the engine: both guards, both invariants, every addressed replica count."""
    report = await Harness(harness_config).run()

    failures = [check for check in report.checks if not check.passed]
    assert failures == [], "\n".join(f"{c.description}: {c.detail}" for c in failures)
    assert report.passed
    assert report.violations == 0
    assert report.rounds_with_a_violation == 0
    assert len(report.rounds) == len(Harness(harness_config).scenarios())
    assert all(result.reconciliation.verdict is Verdict.INVARIANT_HELD for result in report.rounds)
    # Both replica counts were covered, because "does it hold with a second process writing the
    # same rows?" is a different question from "does it hold at all?".
    assert {result.scenario.replicas for result in report.rounds} == set(
        range(1, len(harness_config.runner.replica_urls) + 1)
    )
