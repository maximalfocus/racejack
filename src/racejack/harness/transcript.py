"""Rendering the harness output.

Two audiences, one set of facts. Standard output gets the reconciliation, the interleaving timeline,
and the verdicts, so a reader can follow the run without opening anything. The transcript artifact
additionally carries every per-request record, because "which replica served which request, and what
did it get back" is the raw material behind every claim the summary makes.

Neither carries a token, a secret, or a personal datum — every identity in here is one of the
demonstration's own fictional buyers. Neither carries any statement about speed: this harness
measures correctness under concurrency, and a throughput or latency number would be both out of
scope and misleading.
"""

from __future__ import annotations

from typing import Final

from ..config import HarnessConfig
from ..httpclient import RequestRecord
from ..instrumentation import EVENT_CHECK
from .engine import HarnessReport, RoundResult
from .ledger import ReproductionMode, Variant, Verdict

RULE: Final = "=" * 78
THIN: Final = "-" * 78

TIMELINE_LIMIT: Final = 12

INSTRUMENTATION_NOTE: Final = (
    " The window between the check and the act is a genuine property of this code. The instrumented"
    " synchronization point does not create it — it only holds it open long enough that the"
    " interleaving is the same on every machine. The natural reproduction mode runs the identical"
    " code with nothing attached at all, and the race still happens; that is the evidence, and this"
    " is only the microscope."
)

BACKSTOP_NOTE: Final = (
    " The database-enforced backstop has been removed for this run, deliberately and by name"
    " (drops_units_sold_within_availability, redemptions_single_use). With it in place an"
    " unguarded"
    " write does not oversell the drop — it fails as a constraint violation, and the store returns"
    " an error instead of a negative remaining count. That is exactly what a backstop is for. It is"
    " also why showing the application-level damage requires taking it off first and saying so."
)


def _outcome(record: RequestRecord) -> str:
    if record.refused:
        return "refused"
    if record.succeeded:
        body = record.body or {}
        return str(body.get("status", "ok"))
    return f"error {record.status_code}"


def _header(report: HarnessReport, config: HarnessConfig) -> list[str]:
    lines = [
        RULE,
        " racejack — concurrent load harness",
        RULE,
        f" application variant  : {report.variant.value}",
        f" reproduction mode    : {report.mode.value}",
        f" replicas available   : {', '.join(report.replica_labels)}",
        f" rounds per scenario  : {report.rounds_per_scenario}",
        f" order concurrency    : {config.order_concurrency} concurrent buyers",
        f" redemption concurrency: {config.redemption_concurrency} concurrent redemptions",
        "",
    ]
    if report.variant is Variant.VULNERABLE:
        lines += [
            " *** INTENTIONALLY VULNERABLE APPLICATION — local educational material only ***",
            "",
            BACKSTOP_NOTE,
            "",
        ]
    if report.mode is ReproductionMode.DETERMINISTIC:
        lines += [" INSTRUMENTED RUN.", INSTRUMENTATION_NOTE, ""]
    else:
        lines += [
            " No instrumentation is attached in this mode: no rendezvous, no recorded step, no"
            " wait.",
            "",
        ]
    lines += [
        " Every outcome below is read back through the store's own HTTP boundary. The harness",
        " measures correctness under concurrency only; it makes no claim about how fast anything",
        " is, and no such claim belongs here.",
        "",
    ]
    if report.references:
        lines += [
            " Canonical-state references, from correct sequential runs of the same request counts:",
            *(f"   {name:<12} {state}" for name, state in sorted(report.references.items())),
            "",
        ]
    return lines


def _timeline_block(result: RoundResult) -> list[str]:
    """The reads that observed the same value, and the writes that followed. That pair *is* it."""
    if not result.racing_groups:
        return []
    lines = ["", "  interleaving timeline — the requests that raced:"]
    for group in result.racing_groups[:2]:
        shown = group[:TIMELINE_LIMIT]
        request_ids = {entry.request_id for entry in shown}
        lines.append(
            f"    {len(group)} requests read {group[0].resource} as {group[0].observed} "
            f"before any of them wrote"
        )
        for entry in result.timeline:
            if entry.request_id not in request_ids:
                continue
            marker = "CHECK" if entry.event == EVENT_CHECK else " ACT "
            detail = f"   {entry.detail}" if entry.detail else ""
            lines.append(
                f"      step {entry.step:>5}  {entry.replica:<7}  {marker}  "
                f"{entry.request_id:<13}  observed={entry.observed}{detail}"
            )
        if len(group) > TIMELINE_LIMIT:
            lines.append(
                f"      … and {len(group) - TIMELINE_LIMIT} more requests read the same value"
            )
    return lines


def _round_block(result: RoundResult, *, with_records: bool) -> list[str]:
    lines = [
        THIN,
        f" {result.scenario.label}",
        f" round {result.index}/{result.total}",
        THIN,
    ]
    lines += [f"  {line}" for line in result.reconciliation.as_lines()]
    served = ", ".join(f"{label}={count}" for label, count in result.served_by.items())
    lines.append(f"  served by            {served}")
    lines.append(f"  canonical state      {result.canonical_state}")
    lines += _timeline_block(result)
    if with_records:
        lines.append("")
        # The request id is the same one the application stamps on its audit event, so a refusal
        # here can be matched to the refusal the store logged.
        lines.append(
            "    seq  request id     operation  buyer        addressed  served_by  status  outcome"
        )
        for record in result.records:
            lines.append(
                f"  {record.sequence:>5}  {record.request_id:<13}  {record.operation:<9}  "
                f"{record.buyer_id:<11}  {record.addressed:<9}  {record.served_by or '-':<9}  "
                f"{record.status_code:>6}  {_outcome(record)}"
            )
    lines.append("")
    return lines


def _secure_verdict(report: HarnessReport) -> list[str]:
    if report.passed:
        return [
            " VERDICT: INVARIANT HELD in every round, under genuine concurrent load.",
            "",
            " Both secure strategies confirmed exactly the units the drop owned — not fewer — and",
            " credited the single-use code exactly once, at one replica and at two, with zero",
            " violations and canonical state byte-for-byte identical to a correct sequential run.",
            "",
            " Read the exactness carefully: it is also what proves the harness is real. A client",
            " that quietly serialized its own requests could not spread a burst across two",
            " replicas, and would not be evidence of anything.",
        ]
    return [
        " VERDICT: FAILED — a secure-side expectation was not met. That is a genuine failure,",
        " never a flake to be retried away.",
    ]


def _deterministic_verdict(report: HarnessReport) -> list[str]:
    if report.passed:
        return [
            " VERDICT: INVARIANT VIOLATED in every round — the defect reproduced,",
            " deterministically.",
            "",
            " Requests read the same value before any of them wrote, and every one then acted on",
            " a fact that was already stale. The store confirmed more orders than it owned units",
            " and reported a negative remaining count; one single-use code was credited many times",
            " over. Nothing here is exotic: this is what check-then-act does under load.",
        ]
    return [
        " VERDICT: FAILED — the deterministic mode did not reproduce what it is required to",
        " reproduce, so this run proves nothing about the defect.",
    ]


def _natural_verdict(report: HarnessReport) -> list[str]:
    if report.variant is Variant.SECURE:
        return _secure_verdict(report)
    violated = report.rounds_with_a_violation
    lines = [
        f" OBSERVED: {violated}/{len(report.rounds)} rounds violated the invariant with no",
        " instrumentation attached whatsoever.",
        "",
    ]
    if violated:
        lines += [
            " That is the point of this mode: the race is a property of the code, not of the",
            " microscope. The deterministic mode makes the interleaving repeatable; it does not",
            " make it possible.",
        ]
    else:
        lines += [
            " VERDICT: INCONCLUSIVE — and inconclusive is not a pass.",
            "",
            " This run happened not to observe a violation. It is not evidence that the code is",
            " correct, and treating it as evidence is exactly the reasoning error this project",
            " exists to correct. A race that did not show up this time is still a race.",
        ]
    return lines


def _verdict_block(report: HarnessReport) -> list[str]:
    lines = [RULE, " Reconciliation", RULE]
    failures = [check for check in report.checks if not check.passed]
    lines.append(f"  checks: {len(report.checks) - len(failures)}/{len(report.checks)} passed")
    for check in failures:
        lines.append(f"  [FAIL] {check.description}")
        lines.append(f"         {check.detail}")
    lines.append(
        f"  observed overrun rate ({report.mode.value} mode): {report.observed_overrun_rate}"
    )
    if report.inconclusive_rounds:
        lines.append(
            f"  rounds reported {Verdict.INCONCLUSIVE.value}: "
            f"{report.inconclusive_rounds}/{len(report.rounds)}"
        )
    lines.append("")
    lines.append(RULE)
    if report.variant is Variant.SECURE:
        lines += _secure_verdict(report)
    elif report.mode is ReproductionMode.DETERMINISTIC:
        lines += _deterministic_verdict(report)
    else:
        lines += _natural_verdict(report)
    lines.append(RULE)
    return lines


def render_summary(report: HarnessReport, config: HarnessConfig) -> str:
    """Standard output: reconciliation, timeline, and verdicts, without per-request records."""
    lines = _header(report, config)
    for result in report.rounds:
        lines += _round_block(result, with_records=False)
    lines += _verdict_block(report)
    return "\n".join(lines)


def render_transcript(report: HarnessReport, config: HarnessConfig) -> str:
    """The run artifact: the same facts, plus every per-request record behind them."""
    lines = _header(report, config)
    for result in report.rounds:
        lines += _round_block(result, with_records=True)
    lines += _verdict_block(report)
    return "\n".join(lines) + "\n"
