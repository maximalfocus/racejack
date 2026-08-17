"""Generating load that is genuinely concurrent.

The difference between this module and a loop that awaits requests one at a time is the entire
reason the harness exists, so it is worth being explicit about how the overlap is produced:

* every request gets its own task, created up front;
* each task's **first** action is to wait on a barrier, so nobody sends early while the event loop
  is still creating the rest;
* when the last task arrives, the barrier releases and all of them leave the starting line
  together.

The number of tasks *is* the bound. The harness never has more requests in flight than the
configured concurrency, that number is explicit configuration, and every request is aimed at the
demonstration's own services on a network with no egress.

Nothing here instruments the application. The barrier lives in the client; the code under test is
untouched, which is what makes the natural reproduction mode honest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..httpclient import RequestRecord

Operation = Callable[[int], Awaitable[RequestRecord]]


async def concurrent_burst(
    *,
    concurrency: int,
    operation: Operation,
    stagger_seconds: float = 0.0,
    wave_size: int | None = None,
) -> list[RequestRecord]:
    """Issue ``concurrency`` requests that genuinely overlap, and return them in issue order.

    Two ways to throttle, both entirely client-side, because that is where a throttle actually
    lives — a rate limit, a queue, a slower caller:

    * ``wave_size`` caps how many requests are **in flight at once**. The burst goes out in waves of
      that size, each wave finishing before the next begins. This is the honest version of "only let
      N through at a time", and it is the one that changes how much damage a race does, because it
      changes how many requests can be at the time-of-check together.
    * ``stagger_seconds`` spaces requests apart in time within a wave.

    Neither closes the window. They only make it narrower.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1; got {concurrency}")
    if stagger_seconds < 0:
        raise ValueError(f"stagger_seconds must not be negative; got {stagger_seconds}")
    wave = concurrency if wave_size is None else wave_size
    if wave < 1:
        raise ValueError(f"wave_size must be at least 1; got {wave}")

    records: list[RequestRecord] = []
    for start in range(0, concurrency, wave):
        size = min(wave, concurrency - start)
        barrier = asyncio.Barrier(size)

        async def one(index: int, barrier: asyncio.Barrier = barrier) -> RequestRecord:
            await barrier.wait()
            if stagger_seconds:
                await asyncio.sleep((index - 1) * stagger_seconds)
            return await operation(index)

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(start + offset + 1)) for offset in range(size)]
        records.extend(task.result() for task in tasks)
    return records


async def sequential_run(*, count: int, operation: Operation) -> list[RequestRecord]:
    """Issue ``count`` requests strictly one at a time — the correct-by-construction reference."""
    if count < 1:
        raise ValueError(f"count must be at least 1; got {count}")
    return [await operation(index) for index in range(1, count + 1)]
