"""The burst really is concurrent, and the reference run really is not.

If these two behaved the same, every other assertion in the harness would be worthless: a client
that quietly serialized its own requests can never construct the interleaving the demonstration is
about, and would pass a vulnerable-side check by accident.
"""

from __future__ import annotations

import asyncio

import pytest

from racejack.harness.burst import concurrent_burst, sequential_run
from racejack.httpclient import RequestRecord

CONCURRENCY = 16


def _record(index: int) -> RequestRecord:
    return RequestRecord(
        sequence=index,
        operation="order",
        buyer_id=f"buyer-{index:04d}",
        addressed="app-a",
        served_by="app-a",
        status_code=201,
        request_id=f"order-{index:05d}",
        body=None,
    )


class _Overlap:
    """Counts how many operations were inside the call at the same moment."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.entered: list[int] = []

    async def operation(self, index: int) -> RequestRecord:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.entered.append(index)
        await asyncio.sleep(0)
        self.in_flight -= 1
        return _record(index)


async def test_a_burst_puts_every_request_in_flight_at_once() -> None:
    overlap = _Overlap()
    records = await concurrent_burst(concurrency=CONCURRENCY, operation=overlap.operation)
    assert overlap.peak == CONCURRENCY
    assert len(records) == CONCURRENCY


async def test_a_sequential_run_never_overlaps() -> None:
    overlap = _Overlap()
    records = await sequential_run(count=CONCURRENCY, operation=overlap.operation)
    assert overlap.peak == 1
    assert [record.sequence for record in records] == list(range(1, CONCURRENCY + 1))


async def test_a_burst_returns_records_in_issue_order() -> None:
    async def operation(index: int) -> RequestRecord:
        # Later requests finish first; the returned order must still be the issue order.
        await asyncio.sleep((CONCURRENCY - index) * 0.001)
        return _record(index)

    records = await concurrent_burst(concurrency=CONCURRENCY, operation=operation)
    assert [record.sequence for record in records] == list(range(1, CONCURRENCY + 1))


async def test_the_bound_is_the_configured_concurrency() -> None:
    overlap = _Overlap()
    await concurrent_burst(concurrency=4, operation=overlap.operation)
    assert overlap.peak == 4
    assert len(overlap.entered) == 4


@pytest.mark.parametrize("bad", [0, -1])
async def test_a_meaningless_concurrency_is_refused(bad: int) -> None:
    overlap = _Overlap()
    with pytest.raises(ValueError, match="at least 1"):
        await concurrent_burst(concurrency=bad, operation=overlap.operation)
    with pytest.raises(ValueError, match="at least 1"):
        await sequential_run(count=bad, operation=overlap.operation)


async def test_a_failing_request_does_not_leave_the_burst_hanging() -> None:
    async def operation(index: int) -> RequestRecord:
        if index == 3:
            raise RuntimeError("the store did not answer")
        return _record(index)

    with pytest.raises(BaseExceptionGroup):
        await concurrent_burst(concurrency=CONCURRENCY, operation=operation)
