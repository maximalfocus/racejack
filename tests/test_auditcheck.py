"""The audit gate itself is tested, because a gate that cannot fail proves nothing."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from racejack.audit import EVENT_NAME, REFUSAL_REASON, RefusedOperation, emit_refusal
from racejack.auditcheck import check

VALID_EVENT = {
    "event": EVENT_NAME,
    "request_id": "demo-order-0013",
    "replica": "app-a",
    "operation": RefusedOperation.PLACE_ORDER.value,
    "resource_type": "drop",
    "resource_id": "DROP-2026-03",
    "outcome": "refused",
    "reason": REFUSAL_REASON,
}


def _stream(*events: Mapping[str, object], noise: str = "") -> str:
    lines = ["INFO: application startup complete.", noise]
    lines.extend(json.dumps(event) for event in events)
    return "\n".join(line for line in lines if line)


def test_a_clean_stream_passes() -> None:
    assert check(_stream(VALID_EVENT), expected=1) == []


def test_the_count_must_match_exactly() -> None:
    assert check(_stream(VALID_EVENT, VALID_EVENT), expected=1) != []
    assert check(_stream(), expected=1) != []


def test_a_disclosed_field_fails() -> None:
    leaky = {**VALID_EVENT, "units_remaining": -3}
    failures = check(_stream(leaky), expected=1)
    assert any("units_remaining" in failure for failure in failures)


def test_a_missing_field_fails() -> None:
    incomplete = {key: value for key, value in VALID_EVENT.items() if key != "request_id"}
    failures = check(_stream(incomplete), expected=1)
    assert any("request_id" in failure for failure in failures)


def test_a_reason_that_distinguishes_refusals_fails() -> None:
    oracle = {**VALID_EVENT, "reason": "lost_the_race"}
    failures = check(_stream(oracle), expected=1)
    assert any("indistinguishable" in failure for failure in failures)


def test_a_token_anywhere_in_the_stream_fails() -> None:
    failures = check(
        _stream(VALID_EVENT, noise="GET / with Bearer racejack-demo-token-0001"), expected=1
    )
    assert any("bearer token" in failure for failure in failures)


def test_the_emitted_event_satisfies_its_own_gate(capsys: pytest.CaptureFixture[str]) -> None:
    emit_refusal(
        request_id="demo-order-0013",
        replica="app-b",
        operation=RefusedOperation.REDEEM_CREDIT_CODE,
        resource_type="credit_code",
        resource_id="KESTREL-WELCOME-2500",
    )
    assert check(capsys.readouterr().out, expected=1) == []
