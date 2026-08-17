"""Run parameters, read from the environment.

Two of these are demonstration parameters rather than deployment detail, and both are documented
run parameters of the demo:

* ``RACEJACK_COUNTER_GUARD`` selects which secure counter strategy an application replica uses by
  default. Both strategies are correct and produce identical client-visible outcomes.
* ``RACEJACK_REPLICAS`` selects how many application replicas the demo runner addresses. The number
  of processes that share the state is part of the mechanism this project exists to teach, so it is
  a first-class parameter and not a deployment knob.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DEFAULT_DATABASE_URL: Final = "postgresql://racejack:racejack-demo-password@db:5432/racejack"
DEFAULT_REPLICA_URLS: Final = ("http://app-a:8000", "http://app-b:8000")


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

    request_timeout_seconds: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RunnerConfig:
        source = os.environ if env is None else env
        available = tuple(
            url.strip()
            for url in source.get("RACEJACK_REPLICA_URLS", ",".join(DEFAULT_REPLICA_URLS)).split(
                ","
            )
            if url.strip()
        )
        if not available:
            raise ValueError("RACEJACK_REPLICA_URLS must name at least one replica")
        replicas = int(source.get("RACEJACK_REPLICAS", str(len(available))))
        if not 1 <= replicas <= len(available):
            raise ValueError(
                f"RACEJACK_REPLICAS must be between 1 and {len(available)}; got {replicas}"
            )
        return cls(
            database_url=source.get("RACEJACK_DATABASE_URL", DEFAULT_DATABASE_URL),
            replica_urls=available[:replicas],
            request_timeout_seconds=float(source.get("RACEJACK_REQUEST_TIMEOUT_SECONDS", "30")),
        )
