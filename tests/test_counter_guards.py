"""Both secure counter strategies hold `units_sold <= units_available`, sequentially.

These are sequential tests, and they prove a sequential property. They deliberately claim nothing
about behaviour under concurrent load — establishing that needs a concurrent load harness, which
does not exist at this stage of the project.
"""

from __future__ import annotations

import pytest

from racejack import fixtures
from racejack.config import CounterGuard
from racejack.demo.client import StorefrontClient

CREATED = 201
REFUSED = 409
NOT_FOUND = 404

GUARDS = [pytest.param(guard, id=guard.value) for guard in CounterGuard]


@pytest.mark.parametrize("guard", GUARDS)
async def test_sells_exactly_the_units_it_owns(
    storefront: StorefrontClient, guard: CounterGuard
) -> None:
    for index in range(1, fixtures.DROP_UNITS_AVAILABLE + 1):
        record = await storefront.place_order(
            fixtures.DROP_ID, buyer_index=index, guard=guard.value
        )
        assert record.status_code == CREATED, f"order {index} was not confirmed: {record}"
        assert record.body is not None
        assert record.body["status"] == "confirmed"
        assert record.body["units"] == 1

    over = await storefront.place_order(
        fixtures.DROP_ID, buyer_index=fixtures.DROP_UNITS_AVAILABLE + 1, guard=guard.value
    )
    assert over.status_code == REFUSED
    assert over.body == {"detail": "request could not be completed"}


@pytest.mark.parametrize("guard", GUARDS)
async def test_store_reports_an_exactly_reconciled_ledger(
    storefront: StorefrontClient, guard: CounterGuard
) -> None:
    for index in range(1, fixtures.DROP_UNITS_AVAILABLE + 2):
        await storefront.place_order(fixtures.DROP_ID, buyer_index=index, guard=guard.value)

    view = await storefront.read_drop(fixtures.DROP_ID)
    assert view.body is not None
    assert view.body["units_available"] == fixtures.DROP_UNITS_AVAILABLE
    assert view.body["units_sold"] == fixtures.DROP_UNITS_AVAILABLE
    assert view.body["units_remaining"] == 0
    assert view.body["orders_confirmed"] == fixtures.DROP_UNITS_AVAILABLE


@pytest.mark.parametrize("guard", GUARDS)
async def test_both_strategies_are_served_by_every_addressed_replica(
    storefront: StorefrontClient, guard: CounterGuard
) -> None:
    served = set()
    for index in range(1, fixtures.DROP_UNITS_AVAILABLE + 1):
        record = await storefront.place_order(
            fixtures.DROP_ID, buyer_index=index, guard=guard.value
        )
        assert record.served_by is not None
        served.add(record.served_by)
    assert served == set(storefront.replica_labels)


async def test_the_two_strategies_produce_identical_client_visible_outcomes(
    storefront: StorefrontClient,
) -> None:
    first = await storefront.place_order(
        fixtures.DROP_ID, buyer_index=1, guard=CounterGuard.CONDITIONAL_WRITE.value
    )
    second = await storefront.place_order(
        fixtures.DROP_ID, buyer_index=2, guard=CounterGuard.PESSIMISTIC_LOCK.value
    )
    assert first.body is not None
    assert second.body is not None
    assert first.body.keys() == second.body.keys()
    assert first.body["status"] == second.body["status"] == "confirmed"
    assert first.body["units"] == second.body["units"] == 1


async def test_an_unknown_guard_name_is_refused_without_selecting_a_default(
    storefront: StorefrontClient,
) -> None:
    record = await storefront.place_order(fixtures.DROP_ID, buyer_index=1, guard="no-guard-at-all")
    assert record.status_code == 400
    view = await storefront.read_drop(fixtures.DROP_ID)
    assert view.body is not None
    assert view.body["units_sold"] == 0


async def test_unknown_drop_is_not_found(storefront: StorefrontClient) -> None:
    record = await storefront.place_order("DROP-DOES-NOT-EXIST", buyer_index=1)
    assert record.status_code == NOT_FOUND
    assert record.body == {"detail": "not found"}
