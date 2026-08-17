"""A scope tripwire for this stage of the project.

The concurrent load harness has landed; the vulnerable ladder has not. Until it does, no
vulnerable entry point and no instrumented synchronization point may exist, the secure application
must never gain an unguarded path, and the *demonstration runner* must stay strictly sequential so
it cannot quietly start claiming a concurrency property it does not test. Each of these is retired
deliberately, by the change that is supposed to introduce what it forbids.
"""

from __future__ import annotations

import re
from pathlib import Path

import racejack

PACKAGE_ROOT = Path(racejack.__file__).resolve().parent

FORBIDDEN_MODULE_NAMES = ("vulnerable", "instrumented")
CONCURRENCY_PRIMITIVES = re.compile(
    r"\b(asyncio\.(?:gather|TaskGroup|Semaphore)|create_task|to_thread)\b"
)


def _python_sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_no_vulnerable_module_exists_yet() -> None:
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in _python_sources()
        if any(name in path.stem for name in FORBIDDEN_MODULE_NAMES)
    ]
    assert offenders == [], f"unexpected modules for this stage: {offenders}"


def test_the_demonstration_runner_issues_no_concurrent_requests_yet() -> None:
    """The runner is sequential by construction, so it can prove nothing about concurrency."""
    offenders = {}
    for path in (PACKAGE_ROOT / "demo").rglob("*.py"):
        matches = CONCURRENCY_PRIMITIVES.findall(path.read_text())
        if matches:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = sorted(set(matches))
    assert offenders == {}, (
        f"concurrency primitives appeared before the load harness slice: {offenders}"
    )


def test_the_secure_package_never_sleeps_between_a_check_and_a_write() -> None:
    """An instrumented synchronization point belongs only in a vulnerable variant, later."""
    secure = PACKAGE_ROOT / "secure"
    offenders = [
        str(path.relative_to(PACKAGE_ROOT))
        for path in secure.rglob("*.py")
        if "sleep" in path.read_text()
    ]
    assert offenders == [], f"the secure application must contain no delay: {offenders}"
