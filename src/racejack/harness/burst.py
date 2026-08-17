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


async def concurrent_burst(*, concurrency: int, operation: Operation) -> list[RequestRecord]:
    """Issue ``concurrency`` requests that genuinely overlap, and return them in issue order."""
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1; got {concurrency}")
    barrier = asyncio.Barrier(concurrency)

    async def one(index: int) -> RequestRecord:
        await barrier.wait()
        return await operation(index)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(index)) for index in range(1, concurrency + 1)]
    return [task.result() for task in tasks]


async def sequential_run(*, count: int, operation: Operation) -> list[RequestRecord]:
    """Issue ``count`` requests strictly one at a time — the correct-by-construction reference."""
    if count < 1:
        raise ValueError(f"count must be at least 1; got {count}")
    return [await operation(index) for index in range(1, count + 1)]
