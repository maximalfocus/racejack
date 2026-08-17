"""Guards for two defects the final acceptance pass found, so neither can come back quietly.

Both were things the automated suite could not have caught, because both were about what the product
*says* and what it *returns* rather than about what it computes. That is exactly the category a
final integrated pass exists to find, and exactly the category that needs a test once found.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import racejack
from racejack.artifacts import REMEDY, persist
from racejack.demo.sequential import Check, Report, summary_lines

PACKAGE_ROOT = Path(racejack.__file__).resolve().parent

# Claims that the project lacks something. Every one of these was true at some point in this
# project's history and is not true now, which is precisely how they got shipped.
STALE_ABSENCE_CLAIM = re.compile(
    r"does not yet (?:have|exist|include)"
    r"|not yet (?:have|exist|built|implemented|available)"
    r"|this stage of the project does not"
    r"|no (?:concurrent load )?harness (?:yet|exists)",
    re.IGNORECASE,
)


def _passing_report() -> Report:
    report = Report()
    report.checks.append(Check("everything held", True))
    return report


def _failing_report() -> Report:
    report = Report()
    report.checks.append(Check("something did not hold", False, "detail"))
    return report


# --- defect 1: the demonstration told the reader the project had no harness ---------------------


def test_the_demo_summary_makes_no_claim_that_the_project_lacks_something() -> None:
    rendered = "\n".join(summary_lines(_passing_report()))
    found = STALE_ABSENCE_CLAIM.findall(rendered)
    assert found == [], f"the demonstration claims the project lacks something: {found}"


def test_the_demo_summary_still_makes_the_point_it_has_to_make() -> None:
    """The lesson is load-bearing; only the claim about what exists was wrong."""
    rendered = "\n".join(summary_lines(_passing_report()))
    assert "sequential run cannot construct" in rendered
    assert "not evidence of correct behaviour under concurrent load" in rendered


def test_the_demo_summary_points_at_the_instrument_that_answers_the_question() -> None:
    rendered = "\n".join(summary_lines(_passing_report()))
    assert "racejack.harness" in rendered or "--rm harness" in rendered
    assert "--rm compare" in rendered


def test_a_failing_demo_still_says_so_plainly() -> None:
    rendered = "\n".join(summary_lines(_failing_report()))
    assert "VERDICT: FAILED" in rendered
    assert "never a flake" in rendered


def test_no_shipped_source_claims_the_project_lacks_something_it_ships() -> None:
    """A repository-wide tripwire, because this defect is a stale sentence, not a stale module."""
    offenders = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        matches = STALE_ABSENCE_CLAIM.findall(path.read_text())
        if matches:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = matches
    assert offenders == {}, f"stale absence claims in shipped output: {offenders}"


# --- defect 2: a passing run exited non-zero because a file could not be written ------------------


def test_a_writable_path_is_written_and_reports_no_failure(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "transcript.txt"
    assert persist(target, "evidence") is None
    assert target.read_text() == "evidence"


def test_an_unwritable_path_reports_a_failure_instead_of_raising(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        failure = persist(locked / "transcript.txt", "evidence")
    finally:
        locked.chmod(0o755)
    assert failure is not None
    assert "could not write" in failure


def test_the_failure_message_tells_the_user_how_to_fix_it(tmp_path: Path) -> None:
    """A warning a reader cannot act on is only noise."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        failure = persist(locked / "transcript.txt", "evidence")
    finally:
        locked.chmod(0o755)
    assert failure is not None
    assert "install -d -m 0777 artifacts" in failure
    assert "The run itself is unaffected" in REMEDY


def test_persisting_never_raises(tmp_path: Path) -> None:
    """It is called after a verdict has been reached; it must not be able to change one."""
    # A path whose parent cannot be created, because a file sits where a directory would go.
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    assert persist(blocker / "deeper" / "transcript.txt", "evidence") is not None


@pytest.mark.parametrize(
    "entry_point",
    ["racejack/harness/__main__.py", "racejack/compare/__main__.py"],
)
def test_no_entry_point_turns_an_artifact_failure_into_a_failed_run(entry_point: str) -> None:
    """The exit status reports the demonstration. It must not report the filesystem."""
    source = (PACKAGE_ROOT.parent / entry_point).read_text()
    assert "persist(" in source, f"{entry_point} does not use the shared artifact writer"
    # The only `return 1` a run may make on its own behalf is about its own outcome.
    for block in source.split("failure = persist(")[1:]:
        tail = block.split("\n\n")[0]
        assert "return 1" not in tail, f"{entry_point} exits non-zero on an artifact failure"
