"""The transcript says what happened, and says nothing it has no business saying.

Two properties are gates rather than style: no bearer token, secret, or personal datum reaches the
artifact, and no statement about speed appears anywhere in it. The second matters because a
concurrency harness is exactly the thing a reader would mistake for a benchmark, and this one
measures correctness only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from racejack.auth import TOKEN_PREFIX
from racejack.config import CounterGuard, HarnessConfig, RunnerConfig
from racejack.harness.engine import HarnessReport, RoundResult, Scenario
from racejack.harness.ledger import (
    Invariant,
    Ledger,
    Reconciliation,
    ReproductionMode,
    Variant,
)
from racejack.harness.transcript import render_summary, render_transcript
from racejack.httpclient import RequestRecord

PERFORMANCE_CLAIM = re.compile(
    r"\b(throughput|latency|latencies|benchmark\w*|per second|requests/s|rps|qps|"
    r"faster|slower|speedup|elapsed|duration|p9[059]|percentile)\b",
    re.IGNORECASE,
)


def _config() -> HarnessConfig:
    return HarnessConfig(
        runner=RunnerConfig(
            database_url="postgresql://racejack:demo@db:5432/racejack",
            replica_urls=("http://app-a:8000", "http://app-b:8000"),
            request_timeout_seconds=30.0,
        ),
        order_concurrency=60,
        redemption_concurrency=40,
        rounds=1,
        transcript_path=Path("/artifacts/harness-transcript.txt"),
    )


def _ledger() -> Ledger:
    return Ledger(
        units_available=12,
        units_sold=12,
        units_remaining=0,
        orders_confirmed=12,
        orders_issued=60,
        orders_refused=48,
        code_face_value_cents=2500,
        redemptions=0,
        redemptions_issued=0,
        redemptions_refused=0,
        total_credited_cents=0,
        wallet_balance_cents=0,
    )


def _report(variant: Variant = Variant.SECURE) -> HarnessReport:
    ledger = _ledger()
    scenario = Scenario(Invariant.COUNTER, CounterGuard.CONDITIONAL_WRITE, 2, 60)
    result = RoundResult(
        scenario=scenario,
        index=1,
        total=1,
        reconciliation=Reconciliation(ledger, Invariant.COUNTER, variant, ReproductionMode.NATURAL),
        served_by={"app-a": 30, "app-b": 30},
        canonical_state=ledger.canonical_state(),
        records=(
            RequestRecord(
                1,
                "order",
                "buyer-0001",
                "app-a",
                "app-a",
                201,
                "order-00001",
                {"status": "confirmed"},
            ),
            RequestRecord(
                2,
                "order",
                "buyer-0002",
                "app-b",
                "app-b",
                409,
                "order-00002",
                {"detail": "request could not be completed"},
            ),
        ),
    )
    report = HarnessReport(
        variant=variant,
        mode=ReproductionMode.NATURAL,
        replica_labels=("app-a", "app-b"),
        rounds_per_scenario=1,
        references={Invariant.COUNTER.value: ledger.canonical_state()},
    )
    report.rounds.append(result)
    report.record("the invariant held", True)
    return report


@pytest.mark.parametrize("render", [render_summary, render_transcript])
def test_no_output_makes_a_performance_claim(render: object) -> None:
    text = render(_report(), _config())  # type: ignore[operator]
    found = PERFORMANCE_CLAIM.findall(text)
    assert found == [], f"the harness must make no performance claim; found {found}"


@pytest.mark.parametrize("render", [render_summary, render_transcript])
def test_no_output_carries_a_token(render: object) -> None:
    assert TOKEN_PREFIX not in render(_report(), _config())  # type: ignore[operator]


def test_the_reconciliation_carries_every_number_it_must() -> None:
    text = render_summary(_report(), _config())
    for expected in (
        "units available",
        "orders confirmed",
        "overrun",
        "code face value",
        "redemptions",
        "credited",
        "VERDICT",
    ):
        assert expected in text, f"the reconciliation omitted {expected!r}"


def test_the_transcript_carries_the_per_request_records_and_the_summary_does_not() -> None:
    transcript = render_transcript(_report(), _config())
    summary = render_summary(_report(), _config())
    assert "order-00001" in transcript
    assert "served_by" in transcript
    assert "order-00001" not in summary


def test_the_transcript_names_the_reproduction_mode_and_denies_instrumentation() -> None:
    text = render_transcript(_report(), _config())
    assert "natural" in text
    assert "no instrumentation in any code path" in text


def test_a_quiet_vulnerable_run_is_never_rendered_as_a_pass() -> None:
    report = _report(variant=Variant.VULNERABLE)
    assert "INCONCLUSIVE" in render_summary(report, _config()).upper()
