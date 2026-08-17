"""The rejection audit event.

When the application refuses a request because the invariant it protects is already satisfied — the
drop is sold out, the code is already redeemed — it emits exactly one structured JSON event to
standard output.

The event is deliberately *generic*. It identifies the refused request and its outcome so an
operator can correlate it, and it says nothing else: no remaining stock, no redemption count, no
timing, no contention detail, no token, and no personal datum. Two refusals that arrived for
completely different reasons — the last unit was sold an hour ago, or it was sold a microsecond ago
by a request that raced this one — produce byte-identical events and byte-identical client
responses. That is the point: neither the log nor the caller gets an oracle.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from typing import Final

EVENT_NAME: Final = "invariant_refusal"
REFUSAL_REASON: Final = "invariant_already_satisfied"
"""One reason for every refusal. "Sold out" and "lost the race" stay indistinguishable."""


class RefusedOperation(StrEnum):
    PLACE_ORDER = "place_order"
    REDEEM_CREDIT_CODE = "redeem_credit_code"


def emit_refusal(
    *,
    request_id: str,
    replica: str,
    operation: RefusedOperation,
    resource_type: str,
    resource_id: str,
) -> None:
    """Write exactly one generic refusal event as a single JSON line on standard output."""
    event = {
        "event": EVENT_NAME,
        "request_id": request_id,
        "replica": replica,
        "operation": operation.value,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "outcome": "refused",
        "reason": REFUSAL_REASON,
    }
    sys.stdout.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
