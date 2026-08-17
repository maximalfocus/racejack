"""The shared HTTP boundary: one client, no scheduling policy.

This module knows how to speak to a replica and how to record what happened. It deliberately does
*not* decide when requests are sent — that is the caller's business, and it is the whole difference
between the sequential demonstration and the concurrent harness.

Two things every record carries, because the demonstration is built on them: the replica that
actually **served** the request, and a caller-chosen request id that matches the application's own
audit event.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from .auth import token_for

REQUEST_ID_HEADER: Final = "X-Request-Id"
REPLICA_HEADER: Final = "X-Racejack-Replica"
GUARD_HEADER: Final = "X-Racejack-Guard"
INSTRUMENTED_WINDOW_HEADER: Final = "X-Racejack-Instrumented-Window"
INSTRUMENTED_WINDOW_HOLD: Final = "hold"


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One request, as observed entirely through the product's own boundary."""

    sequence: int
    operation: str
    buyer_id: str
    addressed: str
    served_by: str | None
    status_code: int
    request_id: str
    body: dict[str, Any] | None

    @property
    def succeeded(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def refused(self) -> bool:
        return self.status_code == httpx.codes.CONFLICT


def replica_label(url: str) -> str:
    """A short human label for a replica base URL (``http://app-a:8000`` -> ``app-a``)."""
    return httpx.URL(url).host


class StorefrontHTTP:
    """Speaks to the addressed replicas. The caller chooses the ordering and the concurrency."""

    def __init__(
        self,
        replica_urls: tuple[str, ...],
        *,
        timeout: float = 30.0,
        max_connections: int = 100,
    ) -> None:
        if not replica_urls:
            raise ValueError("at least one replica URL is required")
        self._replica_urls = replica_urls
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    @property
    def replica_urls(self) -> tuple[str, ...]:
        return self._replica_urls

    @property
    def replica_labels(self) -> tuple[str, ...]:
        return tuple(replica_label(url) for url in self._replica_urls)

    def target_for(self, sequence: int) -> str:
        """Spread requests across the addressed replicas, round robin on the caller's sequence."""
        return self._replica_urls[(sequence - 1) % len(self._replica_urls)]

    async def send(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        sequence: int,
        buyer_index: int | None,
        authorization: str | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> RequestRecord:
        base_url = self.target_for(sequence)
        request_id = f"{operation}-{sequence:05d}"
        headers = {REQUEST_ID_HEADER: request_id}
        if authorization is not None:
            headers["Authorization"] = authorization
        elif buyer_index is not None:
            headers["Authorization"] = f"Bearer {token_for(buyer_index)}"
        if extra_headers:
            headers.update(extra_headers)
        response = await self._client.request(
            method, f"{base_url}{path}", headers=headers, json=json_body
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return RequestRecord(
            sequence=sequence,
            operation=operation,
            buyer_id=f"buyer-{buyer_index:04d}" if buyer_index is not None else "-",
            addressed=replica_label(base_url),
            served_by=response.headers.get(REPLICA_HEADER),
            status_code=response.status_code,
            request_id=request_id,
            body=body if isinstance(body, dict) else None,
        )

    @staticmethod
    def _demonstration_headers(
        *, guard: str | None = None, instrumented: bool = False
    ) -> dict[str, str] | None:
        headers: dict[str, str] = {}
        if guard:
            headers[GUARD_HEADER] = guard
        if instrumented:
            # Deterministic reproduction mode. Absent — the default — nothing runs at all.
            headers[INSTRUMENTED_WINDOW_HEADER] = INSTRUMENTED_WINDOW_HOLD
        return headers or None

    async def place_order(
        self,
        drop_id: str,
        *,
        sequence: int,
        buyer_index: int,
        guard: str | None = None,
        instrumented: bool = False,
    ) -> RequestRecord:
        return await self.send(
            "POST",
            f"/drops/{drop_id}/orders",
            operation="order",
            sequence=sequence,
            buyer_index=buyer_index,
            extra_headers=self._demonstration_headers(guard=guard, instrumented=instrumented),
        )

    async def redeem(
        self,
        *,
        sequence: int,
        code: str,
        wallet_id: str,
        buyer_index: int,
        instrumented: bool = False,
    ) -> RequestRecord:
        return await self.send(
            "POST",
            "/credit/redemptions",
            operation="redeem",
            sequence=sequence,
            buyer_index=buyer_index,
            json_body={"code": code, "wallet_id": wallet_id},
            extra_headers=self._demonstration_headers(instrumented=instrumented),
        )

    async def read_drop(
        self, drop_id: str, *, sequence: int, buyer_index: int = 1
    ) -> RequestRecord:
        return await self.send(
            "GET",
            f"/drops/{drop_id}",
            operation="read-drop",
            sequence=sequence,
            buyer_index=buyer_index,
        )

    async def read_wallet(
        self, wallet_id: str, *, sequence: int, buyer_index: int = 1
    ) -> RequestRecord:
        return await self.send(
            "GET",
            f"/credit/wallets/{wallet_id}",
            operation="read-wallet",
            sequence=sequence,
            buyer_index=buyer_index,
        )

    async def probe_credential(
        self, drop_id: str, *, sequence: int, authorization: str | None
    ) -> RequestRecord:
        """Attempt an order with a deliberately bad (or absent) credential."""
        return await self.send(
            "POST",
            f"/drops/{drop_id}/orders",
            operation="auth-probe",
            sequence=sequence,
            buyer_index=None,
            authorization=authorization,
        )

    async def wait_until_ready(self, *, attempts: int = 60, delay: float = 1.0) -> None:
        """Block until every addressed replica reports healthy."""
        for base_url in self._replica_urls:
            for attempt in range(1, attempts + 1):
                try:
                    response = await self._client.get(f"{base_url}/healthz")
                    if response.status_code == httpx.codes.OK:
                        break
                except httpx.HTTPError:
                    pass
                if attempt == attempts:
                    raise RuntimeError(f"replica never became ready: {base_url}")
                await asyncio.sleep(delay)
