"""Scope tripwires: the vulnerable parts must stay where they belong.

The vulnerable application and its instrumented synchronization point now exist, so the tripwire is
no longer "does this file exist" — it is "is any of it reachable from the secure application". The
answer must stay no. A secure code path that gained an unguarded read-then-write, or a delay, or an
import of the instrumentation, would make every comparison in this project meaningless.
"""

from __future__ import annotations

import re
from pathlib import Path

import racejack

PACKAGE_ROOT = Path(racejack.__file__).resolve().parent
SECURE = PACKAGE_ROOT / "secure"
VULNERABLE = PACKAGE_ROOT / "vulnerable"

CONCURRENCY_PRIMITIVES = re.compile(
    r"\b(asyncio\.(?:gather|TaskGroup|Semaphore|Barrier)|create_task|to_thread)\b"
)
DELAY = re.compile(r"\b(sleep|wait_for|Event|Barrier|Lock)\b")


def _sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def test_the_secure_application_never_imports_the_instrumentation() -> None:
    """The instrumented synchronization point exists only in vulnerable code paths."""
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in _sources(SECURE)
        if "instrumentation" in path.read_text()
    ]
    assert offenders == [], f"the secure application reached for instrumentation: {offenders}"


def test_the_secure_application_contains_no_delay_between_a_check_and_a_write() -> None:
    offenders = {
        str(path.relative_to(PACKAGE_ROOT)): sorted(set(DELAY.findall(path.read_text())))
        for path in _sources(SECURE)
        if DELAY.search(path.read_text())
    }
    assert offenders == {}, f"the secure application must contain no delay: {offenders}"


def test_the_instrumentation_is_named_for_what_it_is() -> None:
    """Nothing about it should be discoverable only by reading the implementation."""
    source = (PACKAGE_ROOT / "instrumentation.py").read_text()
    assert "This is instrumentation" in source
    for table in ("toctou_instrumentation_gate", "toctou_timeline"):
        assert table in source, f"{table} should say what it is in its own name"


def test_only_vulnerable_code_and_the_harness_touch_the_instrumentation() -> None:
    allowed = {"instrumentation.py", "seed.py", "vulnerable/app.py", "vulnerable/shapes.py"}
    offenders = []
    for path in _sources(PACKAGE_ROOT):
        relative = str(path.relative_to(PACKAGE_ROOT))
        if relative in allowed or relative.startswith("harness/"):
            continue
        if "instrumentation" in path.read_text():
            offenders.append(relative)
    assert offenders == [], f"unexpected instrumentation reach: {offenders}"


def test_the_demonstration_runner_issues_no_concurrent_requests() -> None:
    """The sequential runner is sequential by construction, so it claims only sequential things."""
    offenders = {
        str(path.relative_to(PACKAGE_ROOT)): sorted(
            set(CONCURRENCY_PRIMITIVES.findall(path.read_text()))
        )
        for path in _sources(PACKAGE_ROOT / "demo")
        if CONCURRENCY_PRIMITIVES.search(path.read_text())
    }
    assert offenders == {}, f"the sequential demonstration acquired concurrency: {offenders}"


LABELS_ITSELF = ("vulnerable", "unguarded", "deliberately", "intentionally")


def test_every_vulnerable_module_labels_itself() -> None:
    """No file in here should read as ordinary application code to someone who opens it cold."""
    offenders = [
        path.name
        for path in _sources(VULNERABLE)
        if not any(label in path.read_text().lower() for label in LABELS_ITSELF)
    ]
    assert offenders == [], f"unlabelled vulnerable modules: {offenders}"


def test_the_vulnerable_application_warns_against_deploying_it() -> None:
    assert "never deploy" in (VULNERABLE / "app.py").read_text().lower()
    assert "never deploy" in (VULNERABLE / "__init__.py").read_text().lower()
    assert "INTENTIONALLY VULNERABLE" in (VULNERABLE / "app.py").read_text()
