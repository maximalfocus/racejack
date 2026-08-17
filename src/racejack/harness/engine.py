"""The harness engine.

Drives the storefront under genuine concurrent load and judges what came back. Everything in here is
plain async Python with no terminal input of any kind, so a test can call `Harness.run()` directly
and inspect the report rather than scraping output.

What it demands depends entirely on what it is driving, and the difference is the demonstration:

* against the **secure** application the assertion is exact — *exactly* the units the drop owns and
  not fewer, *exactly* one credit, zero violations in every round, canonical state byte-for-byte
  identical to a correct sequential run. That exactness also proves the instrument is real, because
  a client that quietly serialized its own requests could never spread a burst across two replicas;
* against the **vulnerable** application in the deterministic mode the overrun is *required*. This
  is the mode that carries the regression assertion that the defect happens;
* against the **vulnerable** application in the natural mode nothing is required, because nothing
  can be. A run that observes no violation is reported `inconclusive`, never as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import fixtures
from ..config import CounterGuard, HarnessConfig
from ..db import connect
from ..httpclient import RequestRecord, StorefrontHTTP
from ..instrumentation import (
    GateSettings,
    TimelineEntry,
    arm_gate,
    disarm_gate,
    gate_timed_out,
    racing_reads,
    read_timeline,
)
from ..seed import prepare_secure_run, prepare_vulnerable_run, seed
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

UNGUARDED = "unguarded"
SINGLE_USE_GUARD = "unique constraint + one transaction"


@dataclass(frozen=True, slots=True)
class Scenario:
    invariant: Invariant
    shape: str
    guard_header: str | None
    replicas: int
    concurrency: int

    @property
    def label(self) -> str:
        return (
            f"{self.invariant.value} · shape: {self.shape} · "
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
    timeline: tuple[TimelineEntry, ...] = ()
    racing_groups: tuple[tuple[TimelineEntry, ...], ...] = ()
    window_timed_out: bool = False


@dataclass(slots=True)
class HarnessReport:
    variant: Variant
    mode: ReproductionMode
    replica_labels: tuple[str, ...]
    rounds_per_scenario: int
    backstop_removed: bool = False
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
    def inconclusive_rounds(self) -> int:
        return sum(
            1 for result in self.rounds if result.reconciliation.verdict is Verdict.INCONCLUSIVE
        )

    @property
    def requests_issued(self) -> int:
        return sum(len(result.records) for result in self.rounds)

    @property
    def observed_overrun_rate(self) -> str:
        """What was observed, stated as a rate, claiming nothing beyond it."""
        offending = sum(result.reconciliation.overrun for result in self.rounds)
        return (
            f"{self.rounds_with_a_violation}/{len(self.rounds)} rounds, "
            f"{offending}/{self.requests_issued} requests"
        )


class Harness:
    """Runs every scenario for one variant in one reproduction mode. No terminal interaction."""

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

    @property
    def _urls(self) -> tuple[str, ...]:
        return self.config.runner.urls_for(self.variant.value)

    @property
    def _instrumented(self) -> bool:
        """Only a vulnerable run in the deterministic mode holds its window open."""
        return self.variant is Variant.VULNERABLE and self.mode is ReproductionMode.DETERMINISTIC

    def scenarios(self) -> list[Scenario]:
        """Both invariants, every addressed replica count, and every shape this variant offers.

        The replica count runs from one up to however many replicas are addressed, because "does
        this hold with a second process writing the same rows?" is a different question from "does
        this hold at all".
        """
        scenarios: list[Scenario] = []
        for replicas in range(1, len(self._urls) + 1):
            if self.variant is Variant.SECURE:
                for guard in CounterGuard:
                    scenarios.append(
                        Scenario(
                            Invariant.COUNTER,
                            guard.value,
                            guard.value,
                            replicas,
                            self.config.order_concurrency,
                        )
                    )
                scenarios.append(
                    Scenario(
                        Invariant.SINGLE_USE,
                        SINGLE_USE_GUARD,
                        None,
                        replicas,
                        self.config.redemption_concurrency,
                    )
                )
            else:
                scenarios.append(
                    Scenario(
                        Invariant.COUNTER,
                        UNGUARDED,
                        None,
                        replicas,
                        self.config.order_concurrency,
                    )
                )
                scenarios.append(
                    Scenario(
                        Invariant.SINGLE_USE,
                        UNGUARDED,
                        None,
                        replicas,
                        self.config.redemption_concurrency,
                    )
                )
        return scenarios

    async def run(self) -> HarnessReport:
        report = HarnessReport(
            variant=self.variant,
            mode=self.mode,
            replica_labels=self._urls,
            rounds_per_scenario=self.config.rounds,
            backstop_removed=self.variant is Variant.VULNERABLE,
        )
        if self.variant is Variant.SECURE:
            async with self._http(len(self._urls)) as http:
                await http.wait_until_ready()
                report.replica_labels = http.replica_labels
                for invariant in Invariant:
                    report.references[invariant.value] = await self._reference_state(
                        http, invariant
                    )
        else:
            async with self._http(len(self._urls)) as http:
                await http.wait_until_ready()
                report.replica_labels = http.replica_labels

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
            self._urls[:replicas],
            timeout=runner.request_timeout_seconds,
            max_connections=concurrency,
        )

    def _operation(self, http: StorefrontHTTP, scenario: Scenario):  # type: ignore[no-untyped-def]
        if scenario.invariant is Invariant.COUNTER:
            return lambda index: http.place_order(
                fixtures.DROP_ID,
                sequence=index,
                buyer_index=index,
                guard=scenario.guard_header,
                instrumented=self._instrumented,
            )
        return lambda index: http.redeem(
            sequence=index,
            code=fixtures.CREDIT_CODE,
            wallet_id=fixtures.WALLET_ID,
            buyer_index=index,
            instrumented=self._instrumented,
        )

    async def _reference_state(self, http: StorefrontHTTP, invariant: Invariant) -> str:
        """A correct-by-construction sequential run, kept as the canonical-state reference."""
        await prepare_secure_run(self.config.runner.database_url)
        count = (
            self.config.order_concurrency
            if invariant is Invariant.COUNTER
            else self.config.redemption_concurrency
        )
        scenario = Scenario(invariant, "reference", None, len(http.replica_urls), count)
        records = await sequential_run(count=count, operation=self._operation(http, scenario))
        ledger = await self._read_ledger(http, records, count)
        return ledger.canonical_state()

    async def _prepare_round(self, scenario: Scenario) -> None:
        database_url = self.config.runner.database_url
        if self.variant is Variant.SECURE:
            await seed(database_url, create=False)
            return
        await prepare_vulnerable_run(database_url)
        async with connect(database_url) as conn:
            if self._instrumented:
                # Hold the window open for every request that can be inside it at once. Nothing
                # serializes an unguarded shape, so that is the whole burst.
                await arm_gate(conn, GateSettings(expected=scenario.concurrency))
            else:
                await disarm_gate(conn)

    async def _round(self, http: StorefrontHTTP, scenario: Scenario, index: int) -> RoundResult:
        await self._prepare_round(scenario)
        records = await concurrent_burst(
            concurrency=scenario.concurrency, operation=self._operation(http, scenario)
        )
        ledger = await self._read_ledger(http, records, scenario.concurrency)
        served: dict[str, int] = {}
        for record in records:
            label = record.served_by or "unknown"
            served[label] = served.get(label, 0) + 1

        timeline: tuple[TimelineEntry, ...] = ()
        groups: tuple[tuple[TimelineEntry, ...], ...] = ()
        timed_out = False
        if self._instrumented:
            async with connect(self.config.runner.database_url) as conn:
                entries = await read_timeline(conn)
                timed_out = await gate_timed_out(conn)
            timeline = tuple(entries)
            groups = tuple(tuple(group) for group in racing_reads(entries))

        return RoundResult(
            scenario=scenario,
            index=index,
            total=self.config.rounds,
            reconciliation=Reconciliation(ledger, scenario.invariant, self.variant, self.mode),
            served_by=dict(sorted(served.items())),
            canonical_state=ledger.canonical_state(),
            records=tuple(records),
            timeline=timeline,
            racing_groups=groups,
            window_timed_out=timed_out,
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

    # --- judging ------------------------------------------------------------------------------

    def _judge(self, report: HarnessReport, result: RoundResult) -> None:
        if self.variant is Variant.SECURE:
            self._judge_secure_round(report, result)
        elif self.mode is ReproductionMode.DETERMINISTIC:
            self._judge_required_reproduction(report, result)
        else:
            self._judge_natural_observation(report, result)
        if result.scenario.replicas > 1:
            where = self._where(result)
            report.record(
                f"{where} — the burst was spread across every addressed replica",
                len([label for label in result.served_by if label != "unknown"])
                == result.scenario.replicas,
                f"served_by={result.served_by}",
            )

    @staticmethod
    def _where(result: RoundResult) -> str:
        return f"{result.scenario.label} · round {result.index}/{result.total}"

    def _judge_secure_round(self, report: HarnessReport, result: RoundResult) -> None:
        where = self._where(result)
        reconciliation = result.reconciliation
        report.record(
            f"{where} — the invariant held, with no overrun",
            reconciliation.overrun == 0,
            f"overrun={reconciliation.overrun}",
        )
        report.record(
            f"{where} — every legitimate request that could succeed did, with no shortfall",
            reconciliation.shortfall == 0,
            f"shortfall={reconciliation.shortfall}, ledger={reconciliation.summary()}",
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
        report.record(
            f"{where} — verdict is '{Verdict.INVARIANT_HELD.value}'",
            reconciliation.verdict is Verdict.INVARIANT_HELD,
            f"verdict={reconciliation.verdict.value}",
        )

    def _judge_required_reproduction(self, report: HarnessReport, result: RoundResult) -> None:
        """The deterministic mode carries the required assertion that the defect happens."""
        where = self._where(result)
        led = result.reconciliation.ledger
        report.record(
            f"{where} — the instrumented window did not time out, so the interleaving is "
            f"deterministic",
            not result.window_timed_out,
            "the rendezvous hit its safety valve; this run is not reproducible",
        )
        report.record(
            f"{where} — the overrun reproduced",
            result.reconciliation.overrun > 0,
            f"overrun={result.reconciliation.overrun}, ledger={result.reconciliation.summary()}",
        )
        report.record(
            f"{where} — the timeline shows two requests reading the same value before either wrote",
            any(len(group) > 1 for group in result.racing_groups),
            f"groups={[(g[0].resource, g[0].observed, len(g)) for g in result.racing_groups]}",
        )
        if result.scenario.invariant is Invariant.COUNTER:
            report.record(
                f"{where} — the store confirmed more orders than it owned units",
                led.orders_confirmed > led.units_available,
                f"orders_confirmed={led.orders_confirmed} units_available={led.units_available}",
            )
            report.record(
                f"{where} — the store reports a negative remaining count",
                led.units_remaining < 0,
                f"units_remaining={led.units_remaining}",
            )
        else:
            report.record(
                f"{where} — one code was credited more than once",
                led.redemptions > 1,
                f"redemptions={led.redemptions}",
            )
            report.record(
                f"{where} — the wallet exceeds the code's face value by a multiple of it",
                led.wallet_balance_cents == led.redemptions * led.code_face_value_cents
                and led.wallet_balance_cents > led.code_face_value_cents,
                f"wallet={led.wallet_balance_cents} face_value={led.code_face_value_cents} "
                f"redemptions={led.redemptions}",
            )
        report.record(
            f"{where} — verdict is '{Verdict.INVARIANT_VIOLATED.value}'",
            result.reconciliation.verdict is Verdict.INVARIANT_VIOLATED,
            f"verdict={result.reconciliation.verdict.value}",
        )

    def _judge_natural_observation(self, report: HarnessReport, result: RoundResult) -> None:
        """Nothing is required here, because nothing can be. Only the observation is recorded."""
        where = self._where(result)
        verdict = result.reconciliation.verdict
        report.record(
            f"{where} — observed without instrumentation: {verdict.value}",
            True,
            f"overrun={result.reconciliation.overrun}",
        )
        report.record(
            f"{where} — a quiet natural run is never reported as a pass",
            verdict is not Verdict.INVARIANT_HELD,
            f"verdict={verdict.value}",
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
        elif self.mode is ReproductionMode.DETERMINISTIC:
            report.record(
                "every deterministic round reproduced the defect",
                report.rounds_with_a_violation == len(report.rounds),
                f"reproduced in {report.rounds_with_a_violation}/{len(report.rounds)} rounds",
            )
        covered = {
            (result.scenario.invariant, result.scenario.shape, result.scenario.replicas)
            for result in report.rounds
        }
        report.record(
            "every scenario ran at every addressed replica count",
            len(covered) == len(self.scenarios()),
            f"covered={len(covered)} expected={len(self.scenarios())}",
        )
