"""Rendering the comparison.

The table is the deliverable: every scenario on its own line, with the columns that let a reader do
the comparison themselves instead of taking a verdict on trust. The narrative around it says what to
look at, not what to conclude.

Nothing here states how fast anything is. This project measures correctness under concurrency, and a
table with a milliseconds column would invite exactly the wrong reading.
"""

from __future__ import annotations

from typing import Final

from ..harness.transcript import RULE, THIN, outcome_of, timeline_block
from .engine import Comparison, ComparisonRow

COLUMNS: Final = (
    ("variant", "variant"),
    ("guard / shape", "shape"),
    ("mode", "mode"),
    ("rep", "replicas"),
    ("concurrency", "concurrency"),
    ("issued", "issued"),
    ("ok", "confirmed"),
    ("rejected", "rejected"),
    ("ledger", "ledger"),
    ("overrun", "overrun"),
    ("verdict", "verdict"),
)

NUMERIC: Final = frozenset({"replicas", "issued", "confirmed", "rejected", "overrun"})


def _cell(row: ComparisonRow, field: str) -> str:
    return str(getattr(row, field))


def render_table(rows: tuple[ComparisonRow, ...]) -> list[str]:
    if not rows:
        return ["  (nothing to compare)"]
    widths = {
        field: max(len(header), *(len(_cell(row, field)) for row in rows))
        for header, field in COLUMNS
    }
    header = "  ".join(
        name.rjust(widths[field]) if field in NUMERIC else name.ljust(widths[field])
        for name, field in COLUMNS
    )
    lines = [f"  {header}".rstrip(), "  " + "-" * len(header)]
    for row in rows:
        rendered = "  ".join(
            _cell(row, field).rjust(widths[field])
            if field in NUMERIC
            else _cell(row, field).ljust(widths[field])
            for _, field in COLUMNS
        )
        lines.append(f"  {rendered}".rstrip())
    return lines


NARRATIVE: Final = [
    " Read the table by finding two rows that differ in exactly one column.",
    "",
    " * `conditional_write` and `pessimistic_lock` against `unguarded`, at the same concurrency:",
    "   the same burst, the same store, and the difference is entirely whether the check and the",
    "   act are one operation or two.",
    "",
    " * `process_lock` at one replica against `process_lock` at two: the same code, the same",
    "   fixtures, the same burst. The only variable is how many processes share the state — and",
    "   the row where it looks fixed is the dangerous one.",
    "",
    " * `single_transaction` against `unguarded`: identical overruns. A transaction gives",
    "   atomicity and durability; the lost update it would have to prevent is an isolation",
    "   property.",
    "",
    " * the `sequential` rows against everything else: the identical vulnerable code, one request",
    "   at a time, is perfectly correct. That is why this defect ships past review and a green",
    "   suite — and why its verdict reads INCONCLUSIVE rather than SECURE.",
    "",
    " * the throttled rows against each other: fewer requests in flight does less damage, and",
    "   never none. Throttling reduces the probability of a race, never its possibility.",
    "",
    " A verdict of INCONCLUSIVE means vulnerable code that happened not to lose a race this time.",
    " It is not a pass, and reading it as one is the exact reasoning error this project exists to",
    " correct.",
]


def render(comparison: Comparison, *, verbose: bool = False) -> str:
    lines = [
        RULE,
        " racejack — comparison across every scenario",
        RULE,
        "",
        " Fictional storefront, local containers, no egress. This measures correctness under",
        " concurrency only and makes no claim about how fast anything is.",
        "",
    ]
    lines += render_table(comparison.rows)
    lines.append("")
    for note in comparison.skipped:
        lines += [THIN, f" NOT COMPARED: {note}", THIN, ""]
    lines += NARRATIVE
    lines.append("")

    if verbose:
        lines += [RULE, " Per-request records and interleaving timelines", RULE, ""]
        for result in comparison.rounds:
            lines += [
                THIN,
                f" {result.scenario.label}",
                THIN,
            ]
            lines += [f"  {line}" for line in result.reconciliation.as_lines()]
            lines += timeline_block(result)
            lines.append("")
            lines.append(
                "    seq  request id     operation  buyer        addressed  served_by  "
                "status  outcome"
            )
            for record in result.records:
                lines.append(
                    f"  {record.sequence:>5}  {record.request_id:<13}  {record.operation:<9}  "
                    f"{record.buyer_id:<11}  {record.addressed:<9}  {record.served_by or '-':<9}  "
                    f"{record.status_code:>6}  {outcome_of(record)}"
                )
            lines.append("")

    lines.append(RULE)
    return "\n".join(lines)
