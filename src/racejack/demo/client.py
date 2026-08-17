"""A thin HTTP client that addresses the storefront the way a buyer would.

Two things matter here beyond "send a request":

* requests are **distributed across the addressed replicas**, and every record carries the replica
  that actually served it, because the number of processes sharing the state is part of what this
  project demonstrates; and
* every record carries a caller-chosen request id, so a refusal in the output can be matched to the
  application's own audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from ..auth import token_for

REQUEST_ID_HEADER: Final = "X-Request-Id"
REPLICA_HEADER: Final = "X-Racejack-Replica"
GUARD_HEADER: Final = "X-Racejack-Guard"


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


class StorefrontClient:
    """Sequential client over the addressed replicas. Contains no concurrency of its own."""

    def __init__(self, replica_urls: tuple[str, ...], *, timeout: float = 30.0) -> None:
        if not replica_urls:
            raise ValueError("at least one replica URL is required")
        self._replica_urls = replica_urls
        self._client = httpx.AsyncClient(timeout=timeout)
        self._sequence = 0

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
    def replica_labels(self) -> tuple[str, ...]:
        return tuple(replica_label(url) for url in self._replica_urls)

    def _next_target(self) -> tuple[int, str]:
        self._sequence += 1
        return self._sequence, self._replica_urls[(self._sequence - 1) % len(self._replica_urls)]

    async def _send(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        buyer_index: int | None,
        authorization: str | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> RequestRecord:
        sequence, base_url = self._next_target()
        request_id = f"demo-{operation}-{sequence:04d}"
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

    async def place_order(
        self, drop_id: str, *, buyer_index: int, guard: str | None = None
    ) -> RequestRecord:
        return await self._send(
            "POST",
            f"/drops/{drop_id}/orders",
            operation="order",
            buyer_index=buyer_index,
            extra_headers={GUARD_HEADER: guard} if guard else None,
        )

    async def redeem(self, *, code: str, wallet_id: str, buyer_index: int) -> RequestRecord:
        return await self._send(
            "POST",
            "/credit/redemptions",
            operation="redeem",
            buyer_index=buyer_index,
            json_body={"code": code, "wallet_id": wallet_id},
        )

    async def read_drop(self, drop_id: str, *, buyer_index: int = 1) -> RequestRecord:
        return await self._send(
            "GET", f"/drops/{drop_id}", operation="read-drop", buyer_index=buyer_index
        )

    async def read_wallet(self, wallet_id: str, *, buyer_index: int = 1) -> RequestRecord:
        return await self._send(
            "GET",
            f"/credit/wallets/{wallet_id}",
            operation="read-wallet",
            buyer_index=buyer_index,
        )

    async def probe_credential(self, drop_id: str, *, authorization: str | None) -> RequestRecord:
        """Attempt an order with a deliberately bad (or absent) credential."""
        return await self._send(
            "POST",
            f"/drops/{drop_id}/orders",
            operation="auth-probe",
            buyer_index=None,
            authorization=authorization,
        )

    async def wait_until_ready(self, *, attempts: int = 60, delay: float = 1.0) -> None:
        """Block until every addressed replica reports healthy."""
        import asyncio

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
