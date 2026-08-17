"""The sequential client used by the demonstration runner.

A thin ordering policy over the shared HTTP boundary: one request at a time, each one taking the
next replica in turn. It contains no concurrency of its own, deliberately — everything it can prove
is a sequential property.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from ..httpclient import RequestRecord, StorefrontHTTP, replica_label

__all__ = ["RequestRecord", "StorefrontClient", "replica_label"]


class StorefrontClient:
    """Sequential client over the addressed replicas."""

    def __init__(self, replica_urls: tuple[str, ...], *, timeout: float = 30.0) -> None:
        self._http = StorefrontHTTP(replica_urls, timeout=timeout, max_connections=4)
        self._sequence = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.__aexit__(exc_type, exc, tb)

    @property
    def replica_labels(self) -> tuple[str, ...]:
        return self._http.replica_labels

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    async def place_order(
        self, drop_id: str, *, buyer_index: int, guard: str | None = None
    ) -> RequestRecord:
        return await self._http.place_order(
            drop_id, sequence=self._next(), buyer_index=buyer_index, guard=guard
        )

    async def redeem(self, *, code: str, wallet_id: str, buyer_index: int) -> RequestRecord:
        return await self._http.redeem(
            sequence=self._next(), code=code, wallet_id=wallet_id, buyer_index=buyer_index
        )

    async def read_drop(self, drop_id: str, *, buyer_index: int = 1) -> RequestRecord:
        return await self._http.read_drop(drop_id, sequence=self._next(), buyer_index=buyer_index)

    async def read_wallet(self, wallet_id: str, *, buyer_index: int = 1) -> RequestRecord:
        return await self._http.read_wallet(
            wallet_id, sequence=self._next(), buyer_index=buyer_index
        )

    async def probe_credential(self, drop_id: str, *, authorization: str | None) -> RequestRecord:
        return await self._http.probe_credential(
            drop_id, sequence=self._next(), authorization=authorization
        )

    async def wait_until_ready(self, *, attempts: int = 60, delay: float = 1.0) -> None:
        await self._http.wait_until_ready(attempts=attempts, delay=delay)
