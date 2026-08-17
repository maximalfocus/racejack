"""Building the comparison.

Runs the harness across every variant and reproduction mode that is actually available and reduces
each round to one row. There is no terminal interaction anywhere in here: it returns a structure, so
a test can assert on the comparison rather than on the printed page.

If the vulnerable application's opt-in profile is not running, this says so and compares what it
can. It never quietly presents a secure-only table as though it were the whole picture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import httpx

from ..config import HarnessConfig
from ..harness.engine import Harness, HarnessReport, RoundResult
from ..harness.ledger import Invariant, ReproductionMode, Variant, Verdict

VERDICT_LABELS = {
    Verdict.INVARIANT_HELD: "SECURE",
    Verdict.INVARIANT_VIOLATED: "VULNERABLE",
    Verdict.INCONCLUSIVE: "INCONCLUSIVE",
}


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One scenario, reduced to the facts that make it comparable to every other scenario."""

    variant: str
    shape: str
    mode: str
    replicas: int
    concurrency: str
    issued: int
    confirmed: int
    rejected: int
    ledger: str
    overrun: int
    verdict: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    rows: tuple[ComparisonRow, ...]
    rounds: tuple[RoundResult, ...]
    skipped: tuple[str, ...] = ()

    @property
    def secure_rows(self) -> tuple[ComparisonRow, ...]:
        return tuple(row for row in self.rows if row.variant == Variant.SECURE.value)

    @property
    def vulnerable_rows(self) -> tuple[ComparisonRow, ...]:
        return tuple(row for row in self.rows if row.variant == Variant.VULNERABLE.value)


def _concurrency_cell(result: RoundResult) -> str:
    """How the load actually arrived — including any throttle, or the row means nothing."""
    scenario = result.scenario
    if scenario.sequential:
        return "1 (sequential)"
    if scenario.wave_size is not None:
        return f"{scenario.concurrency} in waves of {scenario.wave_size}"
    if scenario.stagger_seconds:
        return f"{scenario.concurrency}, {scenario.stagger_seconds * 1000:g}ms apart"
    return str(scenario.concurrency)


def row_for(result: RoundResult, report: HarnessReport) -> ComparisonRow:
    led = result.reconciliation.ledger
    if result.scenario.invariant is Invariant.COUNTER:
        issued, confirmed = led.orders_issued, led.orders_confirmed
        rejected = led.orders_refused
        ledger = f"sold {led.units_sold} of {led.units_available}"
    else:
        issued = led.redemptions_issued
        confirmed = led.redemptions
        rejected = led.redemptions_refused
        ledger = f"credited {led.total_credited_cents} of {led.code_face_value_cents}"
    concurrency = _concurrency_cell(result)
    return ComparisonRow(
        variant=report.variant.value,
        shape=result.scenario.shape,
        mode=report.mode.value,
        replicas=result.scenario.replicas,
        concurrency=concurrency,
        issued=issued,
        confirmed=confirmed,
        rejected=rejected,
        ledger=ledger,
        overrun=result.reconciliation.overrun,
        verdict=VERDICT_LABELS[result.reconciliation.verdict],
        note=result.scenario.note,
    )


def rows_for(report: HarnessReport) -> list[ComparisonRow]:
    """One row per scenario — the first round of each, since every round of one scenario agrees."""
    seen: set[object] = set()
    rows: list[ComparisonRow] = []
    for result in report.rounds:
        if result.scenario.key in seen:
            continue
        seen.add(result.scenario.key)
        rows.append(row_for(result, report))
    return rows


async def vulnerable_is_available(config: HarnessConfig) -> bool:
    urls = config.runner.vulnerable_replica_urls
    if not urls:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{urls[0]}/healthz")
            return response.status_code == httpx.codes.OK
    except httpx.HTTPError:
        return False


async def build_comparison(config: HarnessConfig) -> Comparison:
    """Run every available variant and mode once, and reduce the result to comparable rows."""
    single_round = replace(config, rounds=1)
    rows: list[ComparisonRow] = []
    rounds: list[RoundResult] = []
    skipped: list[str] = []

    secure = await Harness(
        single_round, variant=Variant.SECURE, mode=ReproductionMode.NATURAL
    ).run()
    rows.extend(rows_for(secure))
    rounds.extend(secure.rounds)

    if await vulnerable_is_available(single_round):
        for mode in (ReproductionMode.DETERMINISTIC, ReproductionMode.NATURAL):
            report = await Harness(single_round, variant=Variant.VULNERABLE, mode=mode).run()
            rows.extend(rows_for(report))
            rounds.extend(report.rounds)
    else:
        skipped.append(
            "The vulnerable application is not running, so only the secure side could be "
            "compared. It is opt-in on purpose: start it with its Compose profile *and* "
            "ALLOW_VULNERABLE_DEMO=true, then run this again."
        )

    return Comparison(rows=tuple(rows), rounds=tuple(rounds), skipped=tuple(skipped))
