"""The harness engine.

Drives the storefront under genuine concurrent load and judges what came back. Everything in here is
plain async Python with no terminal input of any kind, so a test can call `Harness.run()` directly
and inspect the report rather than scraping output.

What it asserts against the secure application is deliberately **exact**: not "no more than twelve
orders", but *exactly* twelve — not fewer — and *exactly* one credit, with zero violations in every
round. That exactness is doing double duty. It says the fix preserves legitimate work rather than
protecting the invariant by refusing valid requests; and it is the strongest available evidence that
the harness really generates concurrency, because a harness that quietly serialized its own requests
would confirm the same twelve and would never distribute them across two replicas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import fixtures
from ..config import CounterGuard, HarnessConfig
from ..httpclient import RequestRecord, StorefrontHTTP
from ..seed import seed
from .burst import concurrent_burst, sequential_run
from .ledger import (
    Invariant,
    Ledger,
    Reconciliation,
    ReproductionMode,
    Variant,
    Verdict,
    ledger_from_views,
)


@dataclass(frozen=True, slots=True)
class Scenario:
    invariant: Invariant
    guard: CounterGuard | None
    replicas: int
    concurrency: int

    @property
    def label(self) -> str:
        guard = self.guard.value if self.guard else "unique constraint + one transaction"
        return (
            f"{self.invariant.value} · guard: {guard} · "
            f"replicas addressed: {self.replicas} · concurrency: {self.concurrency}"
        )


@dataclass(frozen=True, slots=True)
class Check:
    description: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RoundResult:
    scenario: Scenario
    index: int
    total: int
    reconciliation: Reconciliation
    served_by: dict[str, int]
    canonical_state: str
    records: tuple[RequestRecord, ...]


@dataclass(slots=True)
class HarnessReport:
    variant: Variant
    mode: ReproductionMode
    replica_labels: tuple[str, ...]
    rounds_per_scenario: int
    references: dict[str, str] = field(default_factory=dict)
    rounds: list[RoundResult] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    def record(self, description: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(description, passed, detail))

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def violations(self) -> int:
        return sum(result.reconciliation.violations for result in self.rounds)

    @property
    def rounds_with_a_violation(self) -> int:
        return sum(1 for result in self.rounds if result.reconciliation.violations)

    @property
    def requests_issued(self) -> int:
        return sum(len(result.records) for result in self.rounds)

    @property
    def observed_overrun_rate(self) -> str:
        """The natural mode reports what it observed, as a rate, and claims nothing beyond it."""
        offending = sum(result.reconciliation.overrun for result in self.rounds)
        return (
            f"{self.rounds_with_a_violation}/{len(self.rounds)} rounds, "
            f"{offending}/{self.requests_issued} requests"
        )


class Harness:
    """Runs every scenario for a variant and returns a report. No terminal interaction."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        variant: Variant = Variant.SECURE,
        mode: ReproductionMode = ReproductionMode.NATURAL,
    ) -> None:
        self.config = config
        self.variant = variant
        self.mode = mode

    def scenarios(self) -> list[Scenario]:
        """Every scenario this run covers: both guards, both invariants, every replica count.

        The replica count runs from one up to however many replicas the harness is configured to
        address, because "does this hold with a second process writing the same rows?" is a
        different question from "does this hold at all".
        """
        available = len(self.config.runner.replica_urls)
        scenarios: list[Scenario] = []
        for replicas in range(1, available + 1):
            for guard in CounterGuard:
                scenarios.append(
                    Scenario(Invariant.COUNTER, guard, replicas, self.config.order_concurrency)
                )
            scenarios.append(
                Scenario(Invariant.SINGLE_USE, None, replicas, self.config.redemption_concurrency)
            )
        return scenarios

    async def run(self) -> HarnessReport:
        report = HarnessReport(
            variant=self.variant,
            mode=self.mode,
            replica_labels=tuple(self.config.runner.replica_urls),
            rounds_per_scenario=self.config.rounds,
        )
        async with self._http(len(self.config.runner.replica_urls)) as http:
            await http.wait_until_ready()
            report.replica_labels = http.replica_labels
            for invariant in Invariant:
                report.references[invariant.value] = await self._reference_state(http, invariant)

        for scenario in self.scenarios():
            async with self._http(scenario.replicas) as http:
                for index in range(1, self.config.rounds + 1):
                    result = await self._round(http, scenario, index)
                    report.rounds.append(result)
                    self._judge(report, result)
        self._judge_run(report)
        return report

    def _http(self, replicas: int) -> StorefrontHTTP:
        runner = self.config.runner
        concurrency = max(self.config.order_concurrency, self.config.redemption_concurrency)
        return StorefrontHTTP(
            runner.replica_urls[:replicas],
            timeout=runner.request_timeout_seconds,
            max_connections=concurrency,
        )

    async def _reference_state(self, http: StorefrontHTTP, invariant: Invariant) -> str:
        """A correct-by-construction sequential run, kept as the canonical-state reference."""
        await seed(self.config.runner.database_url, create=False)
        if invariant is Invariant.COUNTER:
            count = self.config.order_concurrency
            records = await sequential_run(
                count=count,
                operation=lambda index: http.place_order(
                    fixtures.DROP_ID, sequence=index, buyer_index=index
                ),
            )
        else:
            count = self.config.redemption_concurrency
            records = await sequential_run(
                count=count,
                operation=lambda index: http.redeem(
                    sequence=index,
                    code=fixtures.CREDIT_CODE,
                    wallet_id=fixtures.WALLET_ID,
                    buyer_index=index,
                ),
            )
        ledger = await self._read_ledger(http, records, count)
        return ledger.canonical_state()

    async def _round(self, http: StorefrontHTTP, scenario: Scenario, index: int) -> RoundResult:
        await seed(self.config.runner.database_url, create=False)
        if scenario.invariant is Invariant.COUNTER:
            guard = scenario.guard.value if scenario.guard else None
            records = await concurrent_burst(
                concurrency=scenario.concurrency,
                operation=lambda i: http.place_order(
                    fixtures.DROP_ID, sequence=i, buyer_index=i, guard=guard
                ),
            )
        else:
            records = await concurrent_burst(
                concurrency=scenario.concurrency,
                operation=lambda i: http.redeem(
                    sequence=i,
                    code=fixtures.CREDIT_CODE,
                    wallet_id=fixtures.WALLET_ID,
                    buyer_index=i,
                ),
            )
        ledger = await self._read_ledger(http, records, scenario.concurrency)
        served: dict[str, int] = {}
        for record in records:
            served[record.served_by or "unknown"] = served.get(record.served_by or "unknown", 0) + 1
        return RoundResult(
            scenario=scenario,
            index=index,
            total=self.config.rounds,
            reconciliation=Reconciliation(ledger, scenario.invariant, self.variant, self.mode),
            served_by=dict(sorted(served.items())),
            canonical_state=ledger.canonical_state(),
            records=tuple(records),
        )

    async def _read_ledger(
        self, http: StorefrontHTTP, records: list[RequestRecord], issued: int
    ) -> Ledger:
        drop = await http.read_drop(fixtures.DROP_ID, sequence=issued + 1)
        wallet = await http.read_wallet(fixtures.WALLET_ID, sequence=issued + 2)
        if drop.body is None or wallet.body is None:
            raise RuntimeError("the store did not return its own view; cannot reconcile")
        orders = [record for record in records if record.operation == "order"]
        redemptions = [record for record in records if record.operation == "redeem"]
        return ledger_from_views(
            drop=drop.body,
            wallet=wallet.body,
            code_face_value_cents=fixtures.CREDIT_CODE_AMOUNT_CENTS,
            orders_issued=len(orders),
            orders_refused=sum(1 for record in orders if record.refused),
            redemptions_issued=len(redemptions),
            redemptions_refused=sum(1 for record in redemptions if record.refused),
        )

    def _judge(self, report: HarnessReport, result: RoundResult) -> None:
        where = f"{result.scenario.label} · round {result.index}/{result.total}"
        reconciliation = result.reconciliation
        report.record(
            f"{where} — the invariant held, with no overrun",
            reconciliation.overrun == 0,
            f"overrun={reconciliation.overrun}",
        )
        report.record(
            f"{where} — every legitimate request that could succeed did, with no shortfall",
            reconciliation.shortfall == 0,
            f"shortfall={reconciliation.shortfall}, ledger={reconciliation.as_lines()[0]}",
        )
        report.record(
            f"{where} — no partially applied redemption",
            reconciliation.partial_redemptions == 0,
            f"wallet={reconciliation.ledger.wallet_balance_cents} "
            f"credited={reconciliation.ledger.total_credited_cents}",
        )
        report.record(
            f"{where} — the store's own counter agrees with its own order records",
            reconciliation.ledger_disagreement == 0,
            f"units_sold={reconciliation.ledger.units_sold} "
            f"orders_confirmed={reconciliation.ledger.orders_confirmed}",
        )
        report.record(
            f"{where} — canonical state is byte-for-byte identical to the sequential run",
            result.canonical_state == report.references[result.scenario.invariant.value],
            f"observed={result.canonical_state} "
            f"reference={report.references[result.scenario.invariant.value]}",
        )
        if self.variant is Variant.SECURE:
            report.record(
                f"{where} — verdict is '{Verdict.INVARIANT_HELD.value}'",
                reconciliation.verdict is Verdict.INVARIANT_HELD,
                f"verdict={reconciliation.verdict.value}",
            )
        if result.scenario.replicas > 1:
            report.record(
                f"{where} — the burst was spread across every addressed replica",
                len([label for label in result.served_by if label != "unknown"])
                == result.scenario.replicas,
                f"served_by={result.served_by}",
            )

    def _judge_run(self, report: HarnessReport) -> None:
        if self.variant is Variant.SECURE:
            report.record(
                "across every scenario and every round, the secure application recorded "
                "zero violations",
                report.violations == 0,
                f"violations={report.violations} in "
                f"{report.rounds_with_a_violation}/{len(report.rounds)} rounds",
            )
        covered = {
            (result.scenario.invariant, result.scenario.guard, result.scenario.replicas)
            for result in report.rounds
        }
        report.record(
            "every scenario ran at every addressed replica count",
            len(covered) == len(self.scenarios()),
            f"covered={len(covered)} expected={len(self.scenarios())}",
        )
