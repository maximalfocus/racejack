"""Run parameters, read from the environment.

Two of these are demonstration parameters rather than deployment detail, and both are documented
run parameters of the demo:

* ``RACEJACK_COUNTER_GUARD`` selects which secure counter strategy an application replica uses by
  default. Both strategies are correct and produce identical client-visible outcomes.
* ``RACEJACK_REPLICAS`` selects how many application replicas a runner addresses. The number of
  processes that share the state is part of the mechanism this project exists to teach, so it is a
  first-class parameter and not a deployment knob.

The harness parameters — ``RACEJACK_ORDER_CONCURRENCY``, ``RACEJACK_REDEMPTION_CONCURRENCY``, and
``RACEJACK_ROUNDS`` — are demonstration parameters and explicit safety bounds at the same time. The
load exists to expose a correctness defect, is aimed only at the demonstration's own services on a
network with no egress, and is capped by the number of fictional buyers the fixtures define.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from . import fixtures

DEFAULT_DATABASE_URL: Final = "postgresql://racejack:racejack-demo-password@db:5432/racejack"
DEFAULT_REPLICA_URLS: Final = ("http://app-a:8000", "http://app-b:8000")
DEFAULT_VULNERABLE_REPLICA_URLS: Final = ("http://vuln-a:8000", "http://vuln-b:8000")

DEFAULT_ORDER_CONCURRENCY: Final = 60
"""Sixty concurrent buyers against a twelve-unit drop, as the demonstration describes."""

DEFAULT_REDEMPTION_CONCURRENCY: Final = 40
"""Forty concurrent redemptions of one single-use code."""

DEFAULT_ROUNDS: Final = 3
MAX_ROUNDS: Final = 20
MAX_CONCURRENCY: Final = fixtures.BUYER_COUNT
"""A hard ceiling. The load is bounded by explicit configuration and aimed only at our own services;
one distinct fictional buyer per concurrent request is both the point and the limit."""


class CounterGuard(StrEnum):
    """The two independently correct secure strategies for the counter invariant."""

    CONDITIONAL_WRITE = "conditional_write"
    """The check *is* the write: one UPDATE carrying the availability predicate in its WHERE."""

    PESSIMISTIC_LOCK = "pessimistic_lock"
    """Every writer serializes on the drop row (SELECT ... FOR UPDATE) inside one transaction."""


def parse_counter_guard(raw: str | None, *, default: CounterGuard) -> CounterGuard | None:
    """Parse a guard name. Returns ``default`` for ``None``/empty and ``None`` for a bad value."""
    if raw is None or raw == "":
        return default
    try:
        return CounterGuard(raw.strip().lower())
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Configuration for one application replica."""

    database_url: str
    replica_name: str
    default_counter_guard: CounterGuard
    pool_min_size: int
    pool_max_size: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AppConfig:
        source = os.environ if env is None else env
        raw_guard = source.get("RACEJACK_COUNTER_GUARD", CounterGuard.CONDITIONAL_WRITE.value)
        guard = parse_counter_guard(raw_guard, default=CounterGuard.CONDITIONAL_WRITE)
        if guard is None:
            raise ValueError(
                f"RACEJACK_COUNTER_GUARD must be one of "
                f"{', '.join(g.value for g in CounterGuard)}; got {raw_guard!r}"
            )
        return cls(
            database_url=source.get("RACEJACK_DATABASE_URL", DEFAULT_DATABASE_URL),
            replica_name=source.get("RACEJACK_REPLICA_NAME", "app-a"),
            default_counter_guard=guard,
            pool_min_size=int(source.get("RACEJACK_POOL_MIN_SIZE", "2")),
            pool_max_size=int(source.get("RACEJACK_POOL_MAX_SIZE", "16")),
        )


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Configuration for a process that drives the demonstration from outside the application."""

    database_url: str
    replica_urls: tuple[str, ...]
    """The replicas actually addressed, already narrowed to ``RACEJACK_REPLICAS`` entries."""

    vulnerable_replica_urls: tuple[str, ...]
    """The vulnerable replicas, narrowed the same way. Empty unless that opt-in profile is up."""

    request_timeout_seconds: float

    def urls_for(self, variant: str) -> tuple[str, ...]:
        return self.vulnerable_replica_urls if variant == "vulnerable" else self.replica_urls

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RunnerConfig:
        source = os.environ if env is None else env
        available = _replica_list(source, "RACEJACK_REPLICA_URLS", DEFAULT_REPLICA_URLS)
        if not available:
            raise ValueError("RACEJACK_REPLICA_URLS must name at least one replica")
        vulnerable = _replica_list(
            source, "RACEJACK_VULNERABLE_REPLICA_URLS", DEFAULT_VULNERABLE_REPLICA_URLS
        )
        replicas = int(source.get("RACEJACK_REPLICAS", str(len(available))))
        if not 1 <= replicas <= len(available):
            raise ValueError(
                f"RACEJACK_REPLICAS must be between 1 and {len(available)}; got {replicas}"
            )
        return cls(
            database_url=source.get("RACEJACK_DATABASE_URL", DEFAULT_DATABASE_URL),
            replica_urls=available[:replicas],
            vulnerable_replica_urls=vulnerable[:replicas],
            request_timeout_seconds=float(source.get("RACEJACK_REQUEST_TIMEOUT_SECONDS", "30")),
        )


def _replica_list(
    source: dict[str, str] | Any, name: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        url.strip() for url in source.get(name, ",".join(default)).split(",") if url.strip()
    )


def _bounded_int(source: dict[str, str] | Any, name: str, default: int, ceiling: int) -> int:
    raw = source.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc
    if not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be between 1 and {ceiling}; got {value}")
    return value


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    """Configuration for the concurrent load harness.

    The concurrency levels and the round count are demonstration parameters and explicit safety
    bounds at the same time: the load is generated only to expose a correctness defect, is aimed
    only at the demonstration's own services, and can never exceed the number of fictional buyers
    the fixtures define.
    """

    runner: RunnerConfig
    order_concurrency: int
    redemption_concurrency: int
    rounds: int
    transcript_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HarnessConfig:
        source = os.environ if env is None else env
        return cls(
            runner=RunnerConfig.from_env(env),
            order_concurrency=_bounded_int(
                source, "RACEJACK_ORDER_CONCURRENCY", DEFAULT_ORDER_CONCURRENCY, MAX_CONCURRENCY
            ),
            redemption_concurrency=_bounded_int(
                source,
                "RACEJACK_REDEMPTION_CONCURRENCY",
                DEFAULT_REDEMPTION_CONCURRENCY,
                MAX_CONCURRENCY,
            ),
            rounds=_bounded_int(source, "RACEJACK_ROUNDS", DEFAULT_ROUNDS, MAX_ROUNDS),
            transcript_path=Path(
                source.get("RACEJACK_TRANSCRIPT_PATH", "/artifacts/harness-transcript.txt")
            ),
        )
