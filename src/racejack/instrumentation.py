"""The instrumented synchronization point, and the interleaving timeline it makes legible.

**This is instrumentation.** It exists only inside vulnerable code paths, it is never present in
any secure path, and it changes nothing about whether the race is possible. The window between the
check and the act is a genuine property of check-then-act code; all this does is hold that window
open long enough that the interleaving is identical on every machine and every run, instead of
depending on how your laptop happened to schedule two coroutines. The natural reproduction mode
runs the very same code with none of this attached, and the race still happens — that is the
evidence, and this is only the microscope.

Two mechanical points worth knowing:

* the application runs as two separate processes, so an in-process barrier could not synchronize
  them. The rendezvous lives in the shared database, which is the only thing the replicas have in
  common; and
* it uses its **own** connection, outside the request's transaction. A transactional shape's
  arrival would otherwise be invisible to its peers until it committed, which is exactly too late.

Everything here lives in tables whose names say what they are, and a vulnerable run creates them
explicitly. Nothing in the secure schema knows they exist.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

import psycopg

from .db import Conn

CREATE_INSTRUMENTATION_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS toctou_instrumentation_gate (
    gate_id  TEXT PRIMARY KEY,
    expected  INTEGER NOT NULL,
    arm_at    BIGINT,
    timed_out BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS toctou_instrumentation_arrivals (
    arrival_id BIGSERIAL PRIMARY KEY,
    gate_id    TEXT NOT NULL,
    request_id TEXT NOT NULL,
    replica    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS toctou_timeline (
    step       BIGSERIAL PRIMARY KEY,
    request_id TEXT   NOT NULL,
    replica    TEXT   NOT NULL,
    event      TEXT   NOT NULL,
    resource   TEXT   NOT NULL,
    observed   BIGINT,
    detail     TEXT
);
"""

DROP_INSTRUMENTATION_SCHEMA: Final = """
DROP TABLE IF EXISTS toctou_instrumentation_arrivals;
DROP TABLE IF EXISTS toctou_instrumentation_gate;
DROP TABLE IF EXISTS toctou_timeline;
"""

GATE_ID: Final = "toctou-window"

WINDOW_TIMEOUT_SECONDS: Final = 20.0
"""A safety valve, never a normal path. Reaching it means the run was not deterministic."""

WINDOW_POLL_SECONDS: Final = 0.01

EVENT_CHECK: Final = "check"
"""The read. What was true at this moment is only a claim about the past from here on."""

EVENT_ACT: Final = "act"
"""The write. Everything between this line and the matching check is the window."""


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    step: int
    request_id: str
    replica: str
    event: str
    resource: str
    observed: int | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class GateSettings:
    """How wide to hold the window open, and when.

    ``expected`` is how many requests are let inside the window **at a time**. For an unguarded
    shape with no throttle that is the whole burst, because nothing serializes them; narrowing it
    is exactly what throttling does to a race.
    ``arm_at`` optionally restricts the hold to requests whose check observed a particular value,
    which is how a shape that *does* serialize its own writers is caught at the one moment that
    matters — the last unit.
    """

    expected: int
    arm_at: int | None = None


class Instrumentation:
    """Records the check/act timeline and holds the window open. Vulnerable paths only."""

    def __init__(self, conn: Conn, *, replica: str, enabled: bool) -> None:
        self._conn = conn
        self._replica = replica
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def record(
        self,
        *,
        request_id: str,
        event: str,
        resource: str,
        observed: int | None = None,
        detail: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        await self._conn.execute(
            "INSERT INTO toctou_timeline (request_id, replica, event, resource, observed, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (request_id, self._replica, event, resource, observed, detail),
        )

    async def hold_window_open(self, *, request_id: str, observed: int) -> None:
        """Block until this request's whole batch is inside the window.

        Requests pass through **``expected`` at a time**, not once-and-then-free. That batching is
        what makes a narrowed window mean something: with the width set to the whole burst, every
        request is inside the window together; with it set to eight, they go through eight at a
        time, and the damage falls accordingly. A gate that released once and then let everyone
        through would be measuring nothing but luck.

        Returns immediately when the gate is not configured, or when this request's check did not
        observe the armed value.
        """
        if not self._enabled:
            return
        cursor = await self._conn.execute(
            "SELECT expected, arm_at FROM toctou_instrumentation_gate WHERE gate_id = %s",
            (GATE_ID,),
        )
        gate = await cursor.fetchone()
        if gate is None:
            return
        arm_at = gate["arm_at"]
        if arm_at is not None and observed != int(arm_at):
            return
        expected = int(gate["expected"])

        inserted = await self._conn.execute(
            "INSERT INTO toctou_instrumentation_arrivals (gate_id, request_id, replica)"
            " VALUES (%s, %s, %s) RETURNING arrival_id",
            (GATE_ID, request_id, self._replica),
        )
        arrival = await inserted.fetchone()
        if arrival is None:
            return
        ranked = await self._conn.execute(
            "SELECT count(*) AS rank FROM toctou_instrumentation_arrivals"
            " WHERE gate_id = %s AND arrival_id <= %s",
            (GATE_ID, arrival["arrival_id"]),
        )
        rank_row = await ranked.fetchone()
        rank = int(rank_row["rank"]) if rank_row else 1
        # Wait for this request's own batch to fill, not for some earlier batch to have filled.
        target = -(-rank // expected) * expected

        loop = asyncio.get_running_loop()
        deadline = loop.time() + WINDOW_TIMEOUT_SECONDS
        while True:
            counted = await self._conn.execute(
                "SELECT count(*) AS arrived FROM toctou_instrumentation_arrivals"
                " WHERE gate_id = %s",
                (GATE_ID,),
            )
            row = await counted.fetchone()
            if row is not None and int(row["arrived"]) >= target:
                return
            if loop.time() >= deadline:
                # A safety valve, never a normal path: a run that reaches it is not deterministic,
                # so it is recorded and the harness fails the round rather than quietly continuing.
                await self._conn.execute(
                    "UPDATE toctou_instrumentation_gate SET timed_out = true WHERE gate_id = %s",
                    (GATE_ID,),
                )
                return
            await asyncio.sleep(WINDOW_POLL_SECONDS)


async def create_instrumentation(conn: Conn) -> None:
    await conn.execute(CREATE_INSTRUMENTATION_SCHEMA)


async def drop_instrumentation(conn: Conn) -> None:
    await conn.execute(DROP_INSTRUMENTATION_SCHEMA)


async def arm_gate(conn: Conn, settings: GateSettings) -> None:
    """Reset the rendezvous for one round. Called by the harness, never by the application."""
    async with conn.transaction():
        await conn.execute("DELETE FROM toctou_instrumentation_arrivals")
        await conn.execute("DELETE FROM toctou_timeline")
        await conn.execute(
            "INSERT INTO toctou_instrumentation_gate (gate_id, expected, arm_at, timed_out)"
            " VALUES (%s, %s, %s, false)"
            " ON CONFLICT (gate_id) DO UPDATE SET expected = EXCLUDED.expected,"
            " arm_at = EXCLUDED.arm_at, timed_out = false",
            (GATE_ID, settings.expected, settings.arm_at),
        )


async def disarm_gate(conn: Conn) -> None:
    """Remove the rendezvous entirely — the natural mode runs with nothing attached."""
    async with conn.transaction():
        await conn.execute("DELETE FROM toctou_instrumentation_arrivals")
        await conn.execute("DELETE FROM toctou_instrumentation_gate")
        await conn.execute("DELETE FROM toctou_timeline")


async def gate_timed_out(conn: Conn) -> bool:
    cursor = await conn.execute(
        "SELECT timed_out FROM toctou_instrumentation_gate WHERE gate_id = %s", (GATE_ID,)
    )
    row = await cursor.fetchone()
    return bool(row["timed_out"]) if row else False


async def read_timeline(conn: Conn, *, limit: int = 400) -> list[TimelineEntry]:
    try:
        cursor = await conn.execute(
            "SELECT step, request_id, replica, event, resource, observed, detail"
            " FROM toctou_timeline ORDER BY step LIMIT %s",
            (limit,),
        )
    except psycopg.errors.UndefinedTable:
        return []
    return [
        TimelineEntry(
            step=int(row["step"]),
            request_id=str(row["request_id"]),
            replica=str(row["replica"]),
            event=str(row["event"]),
            resource=str(row["resource"]),
            observed=None if row["observed"] is None else int(row["observed"]),
            detail=None if row["detail"] is None else str(row["detail"]),
        )
        for row in await cursor.fetchall()
    ]


def racing_reads(timeline: list[TimelineEntry]) -> list[list[TimelineEntry]]:
    """Group the checks that observed the same value — each group of two or more *is* the defect."""
    by_observation: dict[tuple[str, int], list[TimelineEntry]] = {}
    for entry in timeline:
        if entry.event != EVENT_CHECK or entry.observed is None:
            continue
        by_observation.setdefault((entry.resource, entry.observed), []).append(entry)
    return [group for group in by_observation.values() if len(group) > 1]
