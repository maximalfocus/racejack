"""The fixtures are deterministic and conspicuously fictional."""

from __future__ import annotations

import re

from racejack import auth, fixtures

REAL_LOOKING = re.compile(r"\b(?:\d{13,19}|[\w.+-]+@[\w-]+\.[a-z]{2,})\b", re.IGNORECASE)


def test_the_headline_fixtures_are_the_documented_ones() -> None:
    assert fixtures.DROP_ID == "DROP-2026-03"
    assert fixtures.DROP_PRODUCT_NAME == "Tidewater Field Jacket"
    assert fixtures.DROP_UNITS_AVAILABLE == 12
    assert fixtures.CREDIT_CODE == "KESTREL-WELCOME-2500"
    assert fixtures.CREDIT_CODE_AMOUNT_CENTS == 2500


def test_buyer_identifiers_and_names_are_stable() -> None:
    assert fixtures.buyer_id(1) == "buyer-0001"
    assert fixtures.buyer_id(96) == "buyer-0096"
    assert [fixtures.display_name(i) for i in (1, 17, 33)] == [
        fixtures.display_name(i) for i in (1, 17, 33)
    ]
    assert fixtures.display_name(1) == "Avery Ashcroft"
    assert fixtures.display_name(17) == "Avery Barrowman"


def test_buyer_identifiers_are_unique() -> None:
    identifiers = [buyer.buyer_id for buyer in fixtures.BUYERS]
    assert len(identifiers) == len(set(identifiers))


def test_there_are_enough_buyers_for_the_largest_planned_burst() -> None:
    # The demonstration's largest documented burst is sixty concurrent buyers.
    assert fixtures.BUYER_COUNT >= 60


def test_every_token_is_conspicuously_a_demo_token() -> None:
    for token in auth.TOKENS:
        assert token.startswith("racejack-demo-token-")


def test_every_token_maps_to_a_seeded_buyer() -> None:
    seeded = {buyer.buyer_id for buyer in fixtures.BUYERS}
    assert {record.buyer_id for record in auth.TOKENS.values()} <= seeded


def test_exactly_one_token_is_expired() -> None:
    expired = [record for record in auth.TOKENS.values() if record.expires_at is not None]
    assert len(expired) == 1
    assert expired[0].token == auth.EXPIRED_TOKEN


def test_no_fixture_looks_like_real_personal_or_payment_data() -> None:
    corpus = "\n".join(
        [
            fixtures.STORE_NAME,
            fixtures.DROP_ID,
            fixtures.DROP_PRODUCT_NAME,
            fixtures.CREDIT_CODE,
            fixtures.WALLET_ID,
            *(buyer.display_name for buyer in fixtures.BUYERS),
            *auth.TOKENS,
        ]
    )
    assert REAL_LOOKING.search(corpus) is None
