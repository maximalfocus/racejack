"""The harness engine.

Drives the storefront under genuine concurrent load and judges what came back. Everything in here is
plain async Python with no terminal input of any kind, so a test can call `Harness.run()` directly
and inspect the report rather than scraping output.

What it demands depends entirely on what it is driving, and those differences *are* the
demonstration:

* against the **secure** application the assertion is exact — *exactly* the units the drop owns and
  not fewer, *exactly* one credit, zero violations in every round, canonical state byte-for-byte
  identical to a correct sequential run;
* against the **vulnerable** application in the deterministic mode the overrun is *required* —
  except for the shape that is supposed to look fixed, the process-scoped lock at one replica, where
  zero overruns are required instead, and the very same shape at two replicas must break;
* driven **sequentially**, the identical vulnerable code is required to be exactly correct. That is
  the control, and it is the reason this class of defect ships at all;
* against the **vulnerable** application in the natural mode nothing is required, because nothing
  can be. A run that observes no violation is reported `inconclusive`, never as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

from .. import fixtures
from ..config import CounterGuard, HarnessConfig, VulnerableShape
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
from ..seed import pre_sell_units, prepare_secure_run, prepare_vulnerable_run, seed
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

SINGLE_USE_GUARD = "unique constraint + one transaction"

MODELLED_THROTTLE_STEPS = (8, 2)
"""How many requests a throttled client keeps in flight at once, narrower than the full burst.

Throttling — a rate limit, an added delay, a queue, a slower client — does exactly one thing to a
race: it reduces how many requests can be inside the window at the same moment. Setting that number
explicitly — a client that keeps only `w` requests in flight — models it precisely and makes the
resulting damage a fact rather than a coin flip. Nothing in the application changes; the throttle is
entirely on the caller's side, which is where a throttle lives. The delay-based version runs too, in
the natural mode, and reports what it observed.

The series runs on the **single-use** invariant deliberately. There the arithmetic is exact and
monotone — the first batch of `w` requests all read "not redeemed" and all redeem, so the overrun is
`w - 1`, for every `w`. On the counter the same model produces a sawtooth, because a window width
that happens to divide the remaining stock lands the batches exactly on the boundary. That is an
arithmetic coincidence of the model rather than anything true about throttling, and a control should
not be built on one.
"""

APPLIED_THROTTLE_STEPS = (0.002, 0.010)
"""Real client-side delays between request launches, in seconds.

The unthrottled baseline is the plain scenario these are compared against, so it is not repeated
here with a delay of zero.
"""


class Expectation(StrEnum):
    SECURE_HOLDS = "the invariant must hold"
    DEFECT_REQUIRED = "the defect must reproduce"
    LOOKS_FIXED = "no overrun — only one process shares the state"
    SEQUENTIAL_CORRECT = "sequential execution must be exactly correct"
    OBSERVE_ONLY = "observed; nothing required"


@dataclass(frozen=True, slots=True)
class Scenario:
    invariant: Invariant
    shape: str
    replicas: int
    concurrency: int
    expectation: Expectation
    guard_header: str | None = None
    shape_header: str | None = None
    gate: GateSettings | None = None
    units_pre_sold: int | None = None
    stagger_seconds: float = 0.0
    wave_size: int | None = None
    sequential: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        parts = [
            f"{self.invariant.value} · shape: {self.shape}",
            f"replicas addressed: {self.replicas}",
            "sequential" if self.sequential else f"concurrency: {self.concurrency}",
        ]
        if self.wave_size is not None:
            parts.append(f"in flight at once: {self.wave_size}")
        elif self.gate is not None:
            parts.append(f"window holds: {self.gate.expected}")
        if self.stagger_seconds:
            parts.append(f"throttle: {self.stagger_seconds * 1000:g}ms")
        return " · ".join(parts)

    @property
    def key(self) -> tuple[str, str, int, int, float, bool]:
        return (
            self.invariant.value,
            self.shape,
            self.replicas,
            self.gate.expected if self.gate else 0,
            self.stagger_seconds,
            self.sequential,
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
    isolation_levels: tuple[str, ...] = ()


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

    # --- what to run ---------------------------------------------------------------------------

    def scenarios(self) -> list[Scenario]:
        available = len(self._urls)
        if self.variant is Variant.SECURE:
            return self._secure_scenarios(available)
        if self.mode is ReproductionMode.DETERMINISTIC:
            return self._deterministic_scenarios(available)
        return self._natural_scenarios(available)

    def _secure_scenarios(self, available: int) -> list[Scenario]:
        orders, credits = self.config.order_concurrency, self.config.redemption_concurrency
        scenarios: list[Scenario] = []
        for replicas in range(1, available + 1):
            for guard in CounterGuard:
                scenarios.append(
                    Scenario(
                        Invariant.COUNTER,
                        guard.value,
                        replicas,
                        orders,
                        Expectation.SECURE_HOLDS,
                        guard_header=guard.value,
                    )
                )
            scenarios.append(
                Scenario(
                    Invariant.SINGLE_USE,
                    SINGLE_USE_GUARD,
                    replicas,
                    credits,
                    Expectation.SECURE_HOLDS,
                )
            )
        return scenarios

    def _deterministic_scenarios(self, available: int) -> list[Scenario]:
        orders, credits = self.config.order_concurrency, self.config.redemption_concurrency
        unguarded = VulnerableShape.UNGUARDED.value
        transaction = VulnerableShape.SINGLE_TRANSACTION.value
        scenarios: list[Scenario] = []

        for replicas in range(1, available + 1):
            scenarios.append(
                Scenario(
                    Invariant.COUNTER,
                    unguarded,
                    replicas,
                    orders,
                    Expectation.DEFECT_REQUIRED,
                    shape_header=unguarded,
                    gate=GateSettings(expected=orders),
                )
            )
            scenarios.append(
                Scenario(
                    Invariant.SINGLE_USE,
                    unguarded,
                    replicas,
                    credits,
                    Expectation.DEFECT_REQUIRED,
                    gate=GateSettings(expected=credits),
                )
            )
            scenarios.append(
                Scenario(
                    Invariant.COUNTER,
                    transaction,
                    replicas,
                    orders,
                    Expectation.DEFECT_REQUIRED,
                    shape_header=transaction,
                    gate=GateSettings(expected=orders),
                    note="one transaction at the database's default isolation level",
                )
            )

        # The process-scoped lock. Identical fixture state, identical burst, identical gate rule —
        # hold the window for as many requests as can be at the check at once, which with an
        # in-process lock is exactly one per process. The ONLY variable that differs between these
        # two scenarios is how many processes share the state.
        contested = fixtures.DROP_UNITS_AVAILABLE - 1
        for replicas in range(1, available + 1):
            scenarios.append(
                Scenario(
                    Invariant.COUNTER,
                    VulnerableShape.PROCESS_LOCK.value,
                    replicas,
                    orders,
                    Expectation.LOOKS_FIXED if replicas == 1 else Expectation.DEFECT_REQUIRED,
                    shape_header=VulnerableShape.PROCESS_LOCK.value,
                    gate=GateSettings(expected=replicas, arm_at=contested),
                    units_pre_sold=contested,
                    note="one unit left, one request per process able to be at the check at once",
                )
            )

        # Modelled throttling: fewer requests inside the window at a time, nothing else changed.
        for allowed in MODELLED_THROTTLE_STEPS:
            scenarios.append(
                Scenario(
                    Invariant.SINGLE_USE,
                    unguarded,
                    available,
                    credits,
                    Expectation.DEFECT_REQUIRED,
                    gate=GateSettings(expected=allowed),
                    wave_size=allowed,
                    note="throttled: only this many requests in flight at once, nothing else",
                )
            )

        # The control that explains why this defect ships at all.
        scenarios.append(
            Scenario(
                Invariant.COUNTER,
                unguarded,
                available,
                orders,
                Expectation.SEQUENTIAL_CORRECT,
                shape_header=unguarded,
                sequential=True,
                note="the identical vulnerable code, one request at a time",
            )
        )
        scenarios.append(
            Scenario(
                Invariant.SINGLE_USE,
                unguarded,
                available,
                credits,
                Expectation.SEQUENTIAL_CORRECT,
                sequential=True,
                note="the identical vulnerable code, one request at a time",
            )
        )
        return scenarios

    def _natural_scenarios(self, available: int) -> list[Scenario]:
        orders, credits = self.config.order_concurrency, self.config.redemption_concurrency
        unguarded = VulnerableShape.UNGUARDED.value
        scenarios: list[Scenario] = []
        for replicas in range(1, available + 1):
            scenarios.append(
                Scenario(
                    Invariant.COUNTER,
                    unguarded,
                    replicas,
                    orders,
                    Expectation.OBSERVE_ONLY,
                    shape_header=unguarded,
                )
            )
            scenarios.append(
                Scenario(
                    Invariant.SINGLE_USE,
                    unguarded,
                    replicas,
                    credits,
                    Expectation.OBSERVE_ONLY,
                )
            )
        for stagger in APPLIED_THROTTLE_STEPS:
            scenarios.append(
                Scenario(
                    Invariant.SINGLE_USE,
                    unguarded,
                    available,
                    credits,
                    Expectation.OBSERVE_ONLY,
                    stagger_seconds=stagger,
                    note="an applied client-side throttle: a real delay between request launches",
                )
            )
        return scenarios

    # --- running -------------------------------------------------------------------------------

    async def run(self) -> HarnessReport:
        report = HarnessReport(
            variant=self.variant,
            mode=self.mode,
            replica_labels=self._urls,
            rounds_per_scenario=self.config.rounds,
            backstop_removed=self.variant is Variant.VULNERABLE,
        )
        async with self._http(len(self._urls)) as http:
            await http.wait_until_ready()
            report.replica_labels = http.replica_labels
            if self.variant is Variant.SECURE:
                for invariant in Invariant:
                    report.references[invariant.value] = await self._reference_state(
                        http, invariant
                    )

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
                shape=scenario.shape_header,
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
        scenario = Scenario(
            invariant, "reference", len(http.replica_urls), count, Expectation.SECURE_HOLDS
        )
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
            if scenario.units_pre_sold:
                await pre_sell_units(conn, scenario.units_pre_sold)
            if self._instrumented and scenario.gate is not None:
                await arm_gate(conn, scenario.gate)
            else:
                await disarm_gate(conn)

    async def _round(self, http: StorefrontHTTP, scenario: Scenario, index: int) -> RoundResult:
        await self._prepare_round(scenario)
        operation = self._operation(http, scenario)
        if scenario.sequential:
            records = await sequential_run(count=scenario.concurrency, operation=operation)
        else:
            records = await concurrent_burst(
                concurrency=scenario.concurrency,
                operation=operation,
                stagger_seconds=scenario.stagger_seconds,
                wave_size=scenario.wave_size,
            )
        ledger = await self._read_ledger(http, records, scenario.concurrency)
        served: dict[str, int] = {}
        for record in records:
            label = record.served_by or "unknown"
            served[label] = served.get(label, 0) + 1

        timeline: tuple[TimelineEntry, ...] = ()
        groups: tuple[tuple[TimelineEntry, ...], ...] = ()
        timed_out = False
        isolation: tuple[str, ...] = ()
        if self._instrumented and scenario.gate is not None:
            async with connect(self.config.runner.database_url) as conn:
                entries = await read_timeline(conn)
                timed_out = await gate_timed_out(conn)
            timeline = tuple(entries)
            groups = tuple(tuple(group) for group in racing_reads(entries))
            isolation = _isolation_levels(entries)

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
            isolation_levels=isolation,
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

    # --- judging -------------------------------------------------------------------------------

    @staticmethod
    def _where(result: RoundResult) -> str:
        return f"{result.scenario.label} · round {result.index}/{result.total}"

    def _judge(self, report: HarnessReport, result: RoundResult) -> None:
        judge = {
            Expectation.SECURE_HOLDS: self._judge_secure_round,
            Expectation.DEFECT_REQUIRED: self._judge_required_reproduction,
            Expectation.LOOKS_FIXED: self._judge_looks_fixed,
            Expectation.SEQUENTIAL_CORRECT: self._judge_sequential_control,
            Expectation.OBSERVE_ONLY: self._judge_natural_observation,
        }[result.scenario.expectation]
        judge(report, result)
        if result.scenario.replicas > 1 and not result.scenario.sequential:
            report.record(
                f"{self._where(result)} — the burst was spread across every addressed replica",
                len([label for label in result.served_by if label != "unknown"])
                == result.scenario.replicas,
                f"served_by={result.served_by}",
            )

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

    def _judge_window(self, report: HarnessReport, result: RoundResult) -> None:
        report.record(
            f"{self._where(result)} — the instrumented window did not hit its safety valve",
            not result.window_timed_out,
            "the rendezvous timed out; this run is not reproducible",
        )

    def _judge_required_reproduction(self, report: HarnessReport, result: RoundResult) -> None:
        """The deterministic mode carries the required assertion that the defect happens."""
        where = self._where(result)
        led = result.reconciliation.ledger
        self._judge_window(report, result)
        report.record(
            f"{where} — the overrun reproduced",
            result.reconciliation.overrun > 0,
            f"overrun={result.reconciliation.overrun}, ledger={result.reconciliation.summary()}",
        )
        report.record(
            f"{where} — the timeline shows requests reading the same value before either wrote",
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
        if result.scenario.shape == VulnerableShape.SINGLE_TRANSACTION.value:
            report.record(
                f"{where} — the isolation level in effect was reported, and is not SERIALIZABLE",
                bool(result.isolation_levels)
                and all("serializable" not in level.lower() for level in result.isolation_levels),
                f"isolation={result.isolation_levels}",
            )
        report.record(
            f"{where} — verdict is '{Verdict.INVARIANT_VIOLATED.value}'",
            result.reconciliation.verdict is Verdict.INVARIANT_VIOLATED,
            f"verdict={result.reconciliation.verdict.value}",
        )

    def _judge_looks_fixed(self, report: HarnessReport, result: RoundResult) -> None:
        """The half-fix, addressed at one replica, where it convincingly appears to work."""
        where = self._where(result)
        led = result.reconciliation.ledger
        self._judge_window(report, result)
        report.record(
            f"{where} — no overrun: behind one process the lock genuinely holds the invariant",
            result.reconciliation.overrun == 0,
            f"overrun={result.reconciliation.overrun}, ledger={result.reconciliation.summary()}",
        )
        report.record(
            f"{where} — the drop sold exactly what it owned, and no more",
            led.orders_confirmed == led.units_available and led.units_remaining == 0,
            f"orders_confirmed={led.orders_confirmed} units_remaining={led.units_remaining}",
        )

    def _judge_sequential_control(self, report: HarnessReport, result: RoundResult) -> None:
        """The identical vulnerable code, one request at a time, must be exactly correct."""
        where = self._where(result)
        led = result.reconciliation.ledger
        report.record(
            f"{where} — the identical vulnerable code produced an exactly correct ledger",
            result.reconciliation.overrun == 0 and result.reconciliation.shortfall == 0,
            f"overrun={result.reconciliation.overrun} "
            f"shortfall={result.reconciliation.shortfall} "
            f"ledger={result.reconciliation.summary()}",
        )
        if result.scenario.invariant is Invariant.COUNTER:
            report.record(
                f"{where} — exactly units_available orders confirmed, and not one more",
                led.orders_confirmed == led.units_available,
                f"orders_confirmed={led.orders_confirmed}",
            )
        else:
            report.record(
                f"{where} — the code was redeemed exactly once",
                led.redemptions == 1 and led.wallet_balance_cents == led.code_face_value_cents,
                f"redemptions={led.redemptions} wallet={led.wallet_balance_cents}",
            )
        report.record(
            f"{where} — the ledger reconciles exactly, which is why this defect ships",
            result.reconciliation.ledger_disagreement == 0
            and result.reconciliation.partial_redemptions == 0,
            f"disagreement={result.reconciliation.ledger_disagreement}",
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
        if self.variant is Variant.VULNERABLE:
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
        if self.variant is Variant.VULNERABLE and self.mode is ReproductionMode.DETERMINISTIC:
            self._judge_lock_contrast(report)
            self._judge_throttle_series(report)
        covered = {result.scenario.key for result in report.rounds}
        report.record(
            "every scenario ran at every addressed replica count",
            len(covered) == len(self.scenarios()),
            f"covered={len(covered)} expected={len(self.scenarios())}",
        )

    def _judge_lock_contrast(self, report: HarnessReport) -> None:
        """The whole point of the half-fix: same code, same fixtures, different process count."""
        lock_rounds = [
            result
            for result in report.rounds
            if result.scenario.shape == VulnerableShape.PROCESS_LOCK.value
        ]
        at_one = [result for result in lock_rounds if result.scenario.replicas == 1]
        at_many = [result for result in lock_rounds if result.scenario.replicas > 1]
        if not (at_one and at_many):
            return
        report.record(
            "the process-scoped lock holds at one replica and breaks at two, with the number of "
            "processes sharing the state as the only variable that changed",
            all(result.reconciliation.overrun == 0 for result in at_one)
            and all(result.reconciliation.overrun > 0 for result in at_many),
            f"one replica overruns={[r.reconciliation.overrun for r in at_one]} "
            f"two replicas overruns={[r.reconciliation.overrun for r in at_many]}",
        )

    def _judge_throttle_series(self, report: HarnessReport) -> None:
        """Throttling narrows the window. It never closes it."""
        widest: dict[int, int] = {}
        for result in report.rounds:
            scenario, gate = result.scenario, result.scenario.gate
            if (
                gate is None
                or gate.arm_at is not None
                or scenario.sequential
                or scenario.invariant is not Invariant.SINGLE_USE
                or scenario.shape != VulnerableShape.UNGUARDED.value
                or scenario.replicas != len(self._urls)
            ):
                continue
            widest[gate.expected] = max(widest.get(gate.expected, 0), result.reconciliation.overrun)
        if len(widest) < 2:
            return
        widths = sorted(widest, reverse=True)
        overruns = [widest[width] for width in widths]
        report.record(
            "the overrun falls as the window narrows and never reaches zero — throttling reduces "
            "the probability of a race, never its possibility",
            all(a > b for a, b in pairwise(overruns)) and all(overrun > 0 for overrun in overruns),
            f"requests inside the window {widths} -> overrun {overruns}",
        )


def _isolation_levels(entries: tuple[TimelineEntry, ...] | list[TimelineEntry]) -> tuple[str, ...]:
    """Pull the isolation level the application reported back out of its own timeline."""
    levels = set()
    for entry in entries:
        if entry.detail and "isolation=" in entry.detail:
            levels.add(entry.detail.split("isolation=", 1)[1].split("·")[0].strip())
    return tuple(sorted(levels))
