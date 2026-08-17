"""The shared store's schema, written as explicit SQL.

Two constraints here are the **database-enforced backstop**:
``drops_units_sold_within_availability`` and ``redemptions_single_use``. They express the same two
invariants the application guards enforce, and they hold even when application code is wrong. They
are a backstop, not the primary control: a
correct application never reaches them, and an application that relies on them turns a race into an
error rather than into correct behaviour. Both are named so they can be discussed — and, in a later
demonstration of what the application-level damage looks like without them, removed deliberately and
visibly rather than by accident.
"""

from __future__ import annotations

from typing import Final

CREATE_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS buyers (
    buyer_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drops (
    drop_id         TEXT PRIMARY KEY,
    product_name    TEXT    NOT NULL,
    units_available INTEGER NOT NULL,
    units_sold      INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT drops_units_available_non_negative CHECK (units_available >= 0),
    CONSTRAINT drops_units_sold_within_availability
        CHECK (units_sold >= 0 AND units_sold <= units_available)
);

CREATE TABLE IF NOT EXISTS orders (
    order_id  UUID PRIMARY KEY,
    drop_id   TEXT NOT NULL REFERENCES drops (drop_id),
    buyer_id  TEXT NOT NULL REFERENCES buyers (buyer_id),
    units     INTEGER NOT NULL DEFAULT 1,
    served_by TEXT NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT orders_single_unit CHECK (units = 1)
);

CREATE INDEX IF NOT EXISTS orders_drop_id_idx ON orders (drop_id);

CREATE TABLE IF NOT EXISTS wallets (
    wallet_id      TEXT PRIMARY KEY,
    owner_buyer_id TEXT   NOT NULL REFERENCES buyers (buyer_id),
    balance_cents  BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT wallets_balance_non_negative CHECK (balance_cents >= 0)
);

CREATE TABLE IF NOT EXISTS credit_codes (
    code         TEXT PRIMARY KEY,
    amount_cents BIGINT NOT NULL,
    CONSTRAINT credit_codes_amount_positive CHECK (amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id UUID PRIMARY KEY,
    code          TEXT   NOT NULL REFERENCES credit_codes (code),
    wallet_id     TEXT   NOT NULL REFERENCES wallets (wallet_id),
    buyer_id      TEXT   NOT NULL REFERENCES buyers (buyer_id),
    amount_cents  BIGINT NOT NULL,
    served_by     TEXT   NOT NULL,
    redeemed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT redemptions_single_use UNIQUE (code)
);

CREATE INDEX IF NOT EXISTS redemptions_wallet_id_idx ON redemptions (wallet_id);
"""

TRUNCATE_ALL: Final = """
TRUNCATE TABLE redemptions, orders, credit_codes, wallets, drops, buyers RESTART IDENTITY CASCADE;
"""

BACKSTOP_CONSTRAINTS: Final = (
    "drops_units_sold_within_availability",
    "redemptions_single_use",
)
