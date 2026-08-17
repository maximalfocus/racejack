"""Demo authentication answers every failure mode identically."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from racejack import fixtures
from racejack.auth import EXPIRED_TOKEN, UNKNOWN_TOKEN, authenticate, token_for
from racejack.demo.client import StorefrontClient

UNAUTHORIZED = 401
CREATED = 201

# Every way a credential can fail. `authenticate` must answer all of them identically.
BAD_CREDENTIALS = [
    pytest.param(None, id="missing"),
    pytest.param("", id="empty"),
    pytest.param(token_for(1), id="malformed-no-scheme"),
    pytest.param(f"Basic {token_for(1)}", id="malformed-wrong-scheme"),
    pytest.param("Bearer", id="malformed-scheme-only"),
    pytest.param("Bearer    ", id="malformed-blank-token"),
    pytest.param(f"Bearer {UNKNOWN_TOKEN}", id="unknown"),
    pytest.param(f"Bearer {EXPIRED_TOKEN}", id="expired"),
]

# The subset an HTTP client will actually put on the wire. `Bearer    ` is rejected by httpx as an
# illegal header value before it leaves the client, so it is covered at the unit level only.
SENDABLE_BAD_CREDENTIALS = [case for case in BAD_CREDENTIALS if case.id != "malformed-blank-token"]


@pytest.mark.parametrize("header", BAD_CREDENTIALS)
def test_authenticate_rejects_every_failure_mode_the_same_way(header: str | None) -> None:
    assert authenticate(header) is None


def test_authenticate_accepts_an_issued_token() -> None:
    assert authenticate(f"Bearer {token_for(1)}") == fixtures.buyer_id(1)


def test_expiry_is_evaluated_against_a_fixed_instant() -> None:
    assert authenticate(f"Bearer {EXPIRED_TOKEN}", now=datetime(2019, 1, 1, tzinfo=UTC)) is not None
    assert authenticate(f"Bearer {EXPIRED_TOKEN}", now=datetime.now(UTC)) is None


@pytest.mark.parametrize("header", SENDABLE_BAD_CREDENTIALS)
async def test_http_boundary_returns_generic_401(
    storefront: StorefrontClient, header: str | None
) -> None:
    record = await storefront.probe_credential(fixtures.DROP_ID, authorization=header)
    assert record.status_code == UNAUTHORIZED
    assert record.body == {"detail": "unauthorized"}


async def test_all_credential_failures_are_byte_identical(storefront: StorefrontClient) -> None:
    rendered = set()
    for header in (
        None,
        token_for(1),
        f"Basic {token_for(1)}",
        f"Bearer {UNKNOWN_TOKEN}",
        f"Bearer {EXPIRED_TOKEN}",
    ):
        record = await storefront.probe_credential(fixtures.DROP_ID, authorization=header)
        rendered.add((record.status_code, json.dumps(record.body, sort_keys=True)))
    assert len(rendered) == 1, f"credential failures are distinguishable: {rendered}"


async def test_rejected_credentials_change_nothing(storefront: StorefrontClient) -> None:
    for header in (None, f"Bearer {UNKNOWN_TOKEN}", f"Bearer {EXPIRED_TOKEN}"):
        await storefront.probe_credential(fixtures.DROP_ID, authorization=header)
    view = await storefront.read_drop(fixtures.DROP_ID)
    assert view.body is not None
    assert view.body["units_sold"] == 0
    assert view.body["orders_confirmed"] == 0


async def test_an_issued_token_can_still_buy(storefront: StorefrontClient) -> None:
    record = await storefront.place_order(fixtures.DROP_ID, buyer_index=1)
    assert record.status_code == CREATED
    assert record.body is not None
    assert record.body["buyer_id"] == fixtures.buyer_id(1)
