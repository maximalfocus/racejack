"""The single-use credit code is redeemed exactly once, and never partially."""

from __future__ import annotations

from racejack import fixtures
from racejack.demo.client import StorefrontClient

CREATED = 201
REFUSED = 409
NOT_FOUND = 404


async def test_first_redemption_credits_the_wallet_once(storefront: StorefrontClient) -> None:
    record = await storefront.redeem(
        code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=1
    )
    assert record.status_code == CREATED
    assert record.body is not None
    assert record.body["amount_cents"] == fixtures.CREDIT_CODE_AMOUNT_CENTS
    assert record.body["wallet_balance_cents"] == fixtures.CREDIT_CODE_AMOUNT_CENTS
    assert record.body["status"] == "credited"


async def test_second_redemption_of_the_same_code_is_refused(
    storefront: StorefrontClient,
) -> None:
    await storefront.redeem(code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=1)
    second = await storefront.redeem(
        code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=2
    )
    assert second.status_code == REFUSED
    assert second.body == {"detail": "request could not be completed"}


async def test_a_refused_redemption_leaves_no_partial_credit(
    storefront: StorefrontClient,
) -> None:
    """The credit and its redemption record share one transaction, so neither survives alone."""
    await storefront.redeem(code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=1)
    for buyer_index in range(2, 8):
        await storefront.redeem(
            code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=buyer_index
        )

    view = await storefront.read_wallet(fixtures.WALLET_ID)
    assert view.body is not None
    assert view.body["redemption_count"] == 1
    assert view.body["balance_cents"] == fixtures.CREDIT_CODE_AMOUNT_CENTS
    assert view.body["total_credited_cents"] == fixtures.CREDIT_CODE_AMOUNT_CENTS
    # The two numbers the ledger must reconcile: what the wallet holds, and what the redemption
    # records say was credited into it.
    assert view.body["balance_cents"] == view.body["total_credited_cents"]


async def test_refusals_for_both_invariants_are_indistinguishable(
    storefront: StorefrontClient,
) -> None:
    for index in range(1, fixtures.DROP_UNITS_AVAILABLE + 1):
        await storefront.place_order(fixtures.DROP_ID, buyer_index=index)
    sold_out = await storefront.place_order(
        fixtures.DROP_ID, buyer_index=fixtures.DROP_UNITS_AVAILABLE + 1
    )

    await storefront.redeem(code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=1)
    already_redeemed = await storefront.redeem(
        code=fixtures.CREDIT_CODE, wallet_id=fixtures.WALLET_ID, buyer_index=2
    )

    assert sold_out.status_code == already_redeemed.status_code == REFUSED
    assert sold_out.body == already_redeemed.body


async def test_unknown_code_or_wallet_is_not_found(storefront: StorefrontClient) -> None:
    unknown_code = await storefront.redeem(
        code="KESTREL-NOT-A-REAL-CODE", wallet_id=fixtures.WALLET_ID, buyer_index=1
    )
    assert unknown_code.status_code == NOT_FOUND

    unknown_wallet = await storefront.redeem(
        code=fixtures.CREDIT_CODE, wallet_id="wallet-does-not-exist", buyer_index=1
    )
    assert unknown_wallet.status_code == NOT_FOUND

    view = await storefront.read_wallet(fixtures.WALLET_ID)
    assert view.body is not None
    assert view.body["balance_cents"] == 0
    assert view.body["redemption_count"] == 0
