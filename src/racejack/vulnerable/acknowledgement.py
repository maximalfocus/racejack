"""The second of the two deliberate actions required to start the vulnerable application.

The first is selecting its opt-in Compose profile. That alone is not an acknowledgement — a profile
can be enabled by copying a command line. This is: an explicit environment variable, checked before
the application is constructed at all, spelled exactly.

It lives in its own module so the check can be imported and tested without constructing the
application, which is precisely what it refuses to allow.
"""

from __future__ import annotations

import os
from typing import Final

ACKNOWLEDGEMENT_VARIABLE: Final = "ALLOW_VULNERABLE_DEMO"
ACKNOWLEDGEMENT_VALUE: Final = "true"


class VulnerableDemoNotAcknowledgedError(RuntimeError):
    """Raised when the vulnerable application is started without its explicit acknowledgement."""


def require_acknowledgement(env: dict[str, str] | None = None) -> None:
    source = os.environ if env is None else env
    if source.get(ACKNOWLEDGEMENT_VARIABLE, "").strip().lower() != ACKNOWLEDGEMENT_VALUE:
        raise VulnerableDemoNotAcknowledgedError(
            "The racejack vulnerable application is intentionally broken educational material and "
            f"refuses to start without {ACKNOWLEDGEMENT_VARIABLE}={ACKNOWLEDGEMENT_VALUE}. "
            "Selecting its opt-in Compose profile is not on its own an acknowledgement."
        )
