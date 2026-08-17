"""The comparison: does the table actually let a reader do the comparison themselves?

The engine is asserted through its structured result, never by scraping the printed page — that is
what "directly testable without terminal-input simulation" has to mean if it is to mean anything.
The rendering is checked separately, for the things a reader would be misled by if they were absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from racejack.compare.engine import Comparison, ComparisonRow, build_comparison
from racejack.compare.table import COLUMNS, render, render_table
from racejack.config import HarnessConfig, RunnerConfig, VulnerableShape
from racejack.harness.ledger import Variant

PERFORMANCE_CLAIM = re.compile(
    r"\b(throughput|latenc(?:y|ies)|benchmark\w*|per second|requests/s|rps|qps|"
    r"speedup|outperform\w*|elapsed|duration|p9[059]|percentile|"
    r"(?:faster|slower|quicker) than)\b",
    re.IGNORECASE,
)

WALKTHROUGH = Path(__file__).resolve().parent.parent / "WALKTHROUGH.md"


@pytest.fixture
def compare_config(
    config: RunnerConfig, vulnerable_urls: tuple[str, ...], tmp_path: Path
) -> HarnessConfig:
    assert vulnerable_urls
    return HarnessConfig(
        runner=config,
        order_concurrency=60,
        redemption_concurrency=40,
        rounds=1,
        transcript_path=tmp_path / "transcript.txt",
    )


@pytest.fixture
async def comparison(compare_config: HarnessConfig) -> Comparison:
    return await build_comparison(compare_config)


# --- the engine ---------------------------------------------------------------------------------


def test_the_comparison_covers_both_variants(comparison: Comparison) -> None:
    assert comparison.secure_rows, "the secure side was not compared"
    assert comparison.vulnerable_rows, "the vulnerable side was not compared"
    assert not comparison.skipped, f"something was skipped: {comparison.skipped}"


def test_every_column_the_contract_requires_is_populated(comparison: Comparison) -> None:
    for row in comparison.rows:
        for _, field in COLUMNS:
            value = getattr(row, field)
            assert value != "" and value is not None, f"{field} is empty in {row}"


def test_the_comparison_covers_every_guard_and_every_shape(comparison: Comparison) -> None:
    shapes = {row.shape for row in comparison.rows}
    assert {shape.value for shape in VulnerableShape} <= shapes
    assert {"conditional_write", "pessimistic_lock"} <= shapes


def test_the_comparison_covers_both_modes_and_both_replica_counts(
    comparison: Comparison,
) -> None:
    assert {row.mode for row in comparison.rows} == {"natural", "deterministic"}
    assert {row.replicas for row in comparison.rows} == {1, 2}


def test_the_comparison_covers_both_controls(comparison: Comparison) -> None:
    assert any("sequential" in row.concurrency for row in comparison.rows)
    assert any("waves of" in row.concurrency for row in comparison.rows)
    assert any("apart" in row.concurrency for row in comparison.rows)


def test_every_secure_row_is_secure_and_has_no_overrun(comparison: Comparison) -> None:
    for row in comparison.secure_rows:
        assert row.overrun == 0, f"a secure row reported an overrun: {row}"
        assert row.verdict == "SECURE", f"a secure row was not called secure: {row}"


def test_a_vulnerable_row_is_never_called_secure(comparison: Comparison) -> None:
    """The verdict for vulnerable code is VULNERABLE or INCONCLUSIVE. Never SECURE."""
    for row in comparison.vulnerable_rows:
        assert row.verdict in {"VULNERABLE", "INCONCLUSIVE"}, row
        if row.overrun:
            assert row.verdict == "VULNERABLE", row


def test_the_lock_rows_differ_only_in_the_replica_count(comparison: Comparison) -> None:
    lock = [
        row for row in comparison.vulnerable_rows if row.shape == VulnerableShape.PROCESS_LOCK.value
    ]
    assert len(lock) == 2, f"expected the lock at both replica counts, got {lock}"
    at_one = next(row for row in lock if row.replicas == 1)
    at_two = next(row for row in lock if row.replicas == 2)
    assert (at_one.mode, at_one.concurrency) == (at_two.mode, at_two.concurrency)
    assert at_one.overrun == 0
    assert at_two.overrun > 0


def test_the_table_lets_a_reader_find_the_contrast(comparison: Comparison) -> None:
    """Two rows that differ in one column and reach opposite verdicts — the whole point."""
    secure = next(row for row in comparison.secure_rows if row.shape == "conditional_write")
    vulnerable = next(
        row
        for row in comparison.vulnerable_rows
        if row.shape == VulnerableShape.UNGUARDED.value
        and row.replicas == secure.replicas
        and row.concurrency == secure.concurrency
        and "sold" in row.ledger
    )
    assert secure.issued == vulnerable.issued
    assert secure.confirmed < vulnerable.confirmed
    assert (secure.overrun, vulnerable.overrun) == (0, 48)
    assert (secure.verdict, vulnerable.verdict) == ("SECURE", "VULNERABLE")


# --- the rendering ------------------------------------------------------------------------------


def test_the_rendered_table_has_a_row_per_scenario_and_a_header(
    comparison: Comparison,
) -> None:
    lines = render_table(comparison.rows)
    assert len(lines) == len(comparison.rows) + 2
    for name, _ in COLUMNS:
        assert name in lines[0], f"the header omits {name!r}"


def test_an_empty_comparison_says_so_rather_than_rendering_nothing() -> None:
    assert "nothing to compare" in "\n".join(render_table(()))


def test_the_rendered_comparison_makes_no_performance_claim(comparison: Comparison) -> None:
    for verbose in (False, True):
        found = PERFORMANCE_CLAIM.findall(render(comparison, verbose=verbose))
        assert found == [], f"the comparison must make no performance claim; found {found}"


def test_verbose_adds_the_per_request_records_and_the_timeline(
    comparison: Comparison,
) -> None:
    plain = render(comparison, verbose=False)
    verbose = render(comparison, verbose=True)
    assert "request id" not in plain
    assert "request id" in verbose
    assert "interleaving timeline" in verbose
    assert len(verbose) > len(plain)


def test_the_narrative_says_what_inconclusive_means(comparison: Comparison) -> None:
    rendered = render(comparison)
    assert "INCONCLUSIVE" in rendered
    assert "not a pass" in rendered


def test_a_secure_only_comparison_says_what_it_could_not_compare() -> None:
    """Degrading honestly matters more than degrading quietly."""
    partial = Comparison(
        rows=(
            ComparisonRow(
                variant=Variant.SECURE.value,
                shape="conditional_write",
                mode="natural",
                replicas=2,
                concurrency="60",
                issued=60,
                confirmed=12,
                rejected=48,
                ledger="sold 12 of 12",
                overrun=0,
                verdict="SECURE",
            ),
        ),
        rounds=(),
        skipped=("The vulnerable application is not running, so only the secure side...",),
    )
    rendered = render(partial)
    assert "NOT COMPARED" in rendered
    assert "vulnerable application is not running" in rendered


# --- the walkthrough ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required",
    [
        pytest.param("time-of-check to time-of-use", id="toctou-explained"),
        pytest.param("claim about the past", id="the-mechanism"),
        pytest.param("CWE-367", id="cwe-367"),
        pytest.param("CWE-362", id="cwe-362"),
        pytest.param("A04:2021", id="owasp-anchor"),
        pytest.param("does not appear in the published A04:2021 CWE list", id="anchoring-caveat"),
        pytest.param("CWE-840", id="business-logic-family"),
        pytest.param("dnsrebindjack", id="sibling-cross-reference"),
        pytest.param("resolution", id="resolution-toctou-distinguished"),
        pytest.param("scope contains every writer", id="lock-rule"),
        pytest.param("isolation", id="atomicity-vs-isolation"),
        pytest.param("SERIALIZABLE", id="serializable-named"),
        pytest.param("version-column compare-and-set", id="optimistic-cas-named"),
        pytest.param("Distributed locks", id="distributed-locks-named"),
        pytest.param("backstop, not the primary control", id="constraints-are-a-backstop"),
        pytest.param("genuine property of the code", id="instrumentation-note"),
        pytest.param("natural", id="natural-mode-is-the-evidence"),
        pytest.param("Sequential execution passes", id="control-1"),
        pytest.param("never reaches zero", id="control-2"),
        pytest.param("atomic conditional write", id="secure-strategy-a"),
        pytest.param("FOR UPDATE", id="secure-strategy-b"),
        pytest.param("must never be", id="do-not-deploy-warning"),
    ],
)
def test_the_walkthrough_covers_what_it_must(required: str) -> None:
    assert WALKTHROUGH.is_file(), "the walkthrough is missing"
    text = WALKTHROUGH.read_text()
    assert required.lower() in text.lower(), f"the walkthrough does not cover {required!r}"


def test_the_walkthrough_makes_no_performance_claim() -> None:
    found = PERFORMANCE_CLAIM.findall(WALKTHROUGH.read_text())
    assert found == [], f"the walkthrough must make no performance claim; found {found}"


def test_the_walkthrough_carries_no_token() -> None:
    assert "racejack-demo-token-" not in WALKTHROUGH.read_text()


def test_the_walkthrough_warns_about_deployment_at_both_ends() -> None:
    """A reader who stops after the first screen must already have been warned."""
    text = " ".join(WALKTHROUGH.read_text().lower().replace(">", " ").split())
    head, tail = text[:900], text[-900:]
    assert "must never be deployed" in head, "the opening does not warn against deploying it"
    assert "do not deploy" in tail, "the closing does not warn against deploying it"
