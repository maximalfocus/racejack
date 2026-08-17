"""The replica count and the counter guard are real, validated run parameters."""

from __future__ import annotations

import pytest

from racejack.config import AppConfig, CounterGuard, RunnerConfig, parse_counter_guard

BASE_ENV = {
    "RACEJACK_DATABASE_URL": "postgresql://racejack:demo@db:5432/racejack",
    "RACEJACK_REPLICA_URLS": "http://app-a:8000,http://app-b:8000",
}


def test_replica_count_narrows_the_addressed_replicas() -> None:
    one = RunnerConfig.from_env({**BASE_ENV, "RACEJACK_REPLICAS": "1"})
    two = RunnerConfig.from_env({**BASE_ENV, "RACEJACK_REPLICAS": "2"})
    assert one.replica_urls == ("http://app-a:8000",)
    assert two.replica_urls == ("http://app-a:8000", "http://app-b:8000")


def test_replica_count_defaults_to_every_running_replica() -> None:
    assert len(RunnerConfig.from_env(BASE_ENV).replica_urls) == 2


@pytest.mark.parametrize("replicas", ["0", "3", "-1"])
def test_an_out_of_range_replica_count_is_refused(replicas: str) -> None:
    with pytest.raises(ValueError, match="RACEJACK_REPLICAS"):
        RunnerConfig.from_env({**BASE_ENV, "RACEJACK_REPLICAS": replicas})


@pytest.mark.parametrize("guard", list(CounterGuard))
def test_every_guard_name_round_trips(guard: CounterGuard) -> None:
    assert parse_counter_guard(guard.value, default=CounterGuard.CONDITIONAL_WRITE) is guard


def test_an_unknown_guard_name_is_rejected_rather_than_defaulted() -> None:
    assert parse_counter_guard("no-such-guard", default=CounterGuard.CONDITIONAL_WRITE) is None


def test_an_absent_guard_header_selects_the_configured_default() -> None:
    assert (
        parse_counter_guard(None, default=CounterGuard.PESSIMISTIC_LOCK)
        is CounterGuard.PESSIMISTIC_LOCK
    )


def test_the_app_refuses_to_start_with_an_unknown_default_guard() -> None:
    with pytest.raises(ValueError, match="RACEJACK_COUNTER_GUARD"):
        AppConfig.from_env({**BASE_ENV, "RACEJACK_COUNTER_GUARD": "no-such-guard"})
