"""Rendering the harness output.

Two audiences, one set of facts. Standard output gets the reconciliation and the verdicts, so a
reader can follow the run without opening anything. The transcript artifact additionally carries
every per-request record, because "which replica served which request, and what did it get back" is
the raw material behind every claim the summary makes.

Neither carries a token, a secret, or a personal datum — every identity in here is one of the
demonstration's own fictional buyers. Neither carries any statement about speed: this harness
measures correctness under concurrency, and a throughput or latency number would be both out of
scope and misleading.
"""

from __future__ import annotations

from typing import Final

from ..config import HarnessConfig
from ..httpclient import RequestRecord
from .engine import HarnessReport, RoundResult

RULE: Final = "=" * 78
THIN: Final = "-" * 78


def _outcome(record: RequestRecord) -> str:
    if record.refused:
        return "refused"
    if record.succeeded:
        body = record.body or {}
        return str(body.get("status", "ok"))
    return f"error {record.status_code}"


def _header(report: HarnessReport, config: HarnessConfig) -> list[str]:
    return [
        RULE,
        " racejack — concurrent load harness",
        RULE,
        f" application variant  : {report.variant.value}",
        f" reproduction mode    : {report.mode.value} — no instrumentation in any code path",
        f" replicas available   : {', '.join(report.replica_labels)}",
        f" rounds per scenario  : {report.rounds_per_scenario}",
        f" order concurrency    : {config.order_concurrency} concurrent buyers",
        f" redemption concurrency: {config.redemption_concurrency} concurrent redemptions",
        "",
        " Every outcome below is read back through the store's own HTTP boundary. The harness",
        " measures correctness under concurrency only; it makes no claim about how fast anything",
        " is, and no such claim belongs here.",
        "",
        " Canonical-state references, from correct sequential runs of the same request counts:",
        *(f"   {name:<12} {state}" for name, state in sorted(report.references.items())),
        "",
    ]


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
    lines.append("")
    lines.append(RULE)
    if report.passed:
        lines += [
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
    else:
        lines += [
            " VERDICT: FAILED — a secure-side expectation was not met. That is a genuine failure,",
            " never a flake to be retried away.",
        ]
    lines.append(RULE)
    return lines


def render_summary(report: HarnessReport, config: HarnessConfig) -> str:
    """Standard output: reconciliation and verdicts, without the per-request records."""
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
