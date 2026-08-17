"""Demo-only bearer authentication.

Authentication exists here for one reason: so that every request in the output carries an identity.
It is **not** the subject of this demonstration, and it deliberately does nothing that could
serialize or deduplicate concurrent requests — no locks, no per-token state, no nonce tracking, no
database round trip. Every token is a conspicuously fake constant compiled into the image.

Missing, malformed, unknown, and expired tokens are all rejected identically, so the response
carries no information about *why* a credential failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from . import fixtures

TOKEN_PREFIX: Final = "racejack-demo-token-"
EXPIRED_TOKEN: Final = "racejack-demo-token-expired"
EXPIRED_AT: Final = datetime(2020, 1, 1, tzinfo=UTC)
"""A fixed instant in the past, so "expired" is deterministic rather than clock-dependent."""


@dataclass(frozen=True, slots=True)
class DemoToken:
    """A fictional bearer credential belonging to a fictional buyer."""

    token: str
    buyer_id: str
    expires_at: datetime | None


def token_for(index: int) -> str:
    """The fictional bearer token for a one-based buyer index."""
    return f"{TOKEN_PREFIX}{index:04d}"


def _build_registry() -> dict[str, DemoToken]:
    registry = {
        token_for(i): DemoToken(token_for(i), fixtures.buyer_id(i), None)
        for i in range(1, fixtures.BUYER_COUNT + 1)
    }
    registry[EXPIRED_TOKEN] = DemoToken(EXPIRED_TOKEN, fixtures.EXPIRED_BUYER_ID, EXPIRED_AT)
    return registry


TOKENS: Final[dict[str, DemoToken]] = _build_registry()

UNKNOWN_TOKEN: Final = "racejack-demo-token-not-issued"
"""A well-formed token that was never issued, used by the demonstration and by tests."""


def authenticate(authorization_header: str | None, *, now: datetime | None = None) -> str | None:
    """Return the fictional buyer id for a valid header, or ``None`` for every failure mode.

    Deliberately one return value for missing, malformed, unknown, and expired credentials: the
    caller must not be able to tell those apart.
    """
    if not authorization_header:
        return None
    scheme, _, presented = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        return None
    record = TOKENS.get(presented.strip())
    if record is None:
        return None
    if record.expires_at is not None and record.expires_at <= (now or datetime.now(UTC)):
        return None
    return record.buyer_id
