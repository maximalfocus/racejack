"""The HTTP boundary both application variants share.

Everything in this module is identical between the secure application and the vulnerable one: the
routes, the credentials, the read views, the refusal responses, the audit event, and above all the
success payloads. That is deliberate. The two variants must be indistinguishable to a client doing
ordinary business, so that the only difference a reader can find is **how the check and the act are
sequenced** — which is why the interesting code lives in `secure/guards.py` and
`vulnerable/shapes.py`, and the boring code lives here, once.

A refusal says nothing about why. "Sold out", "already redeemed", and "lost the race to another
buyer" are the same 409 with the same body and the same audit event; missing, malformed, unknown,
and expired credentials are the same 401 with the same body.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from . import audit
from .auth import authenticate
from .config import AppConfig
from .db import ConnPool
from .models import (
    DropView,
    HealthResponse,
    OrderOutcome,
    OrderResponse,
    OrderResult,
    OrderStatus,
    RedemptionOutcome,
    RedemptionResponse,
    RedemptionResult,
    RedemptionStatus,
    WalletView,
)
from .store import read_drop, read_wallet

REQUEST_ID_HEADER: Final = "X-Request-Id"
REPLICA_HEADER: Final = "X-Racejack-Replica"
GUARD_HEADER: Final = "X-Racejack-Guard"
SHAPE_HEADER: Final = "X-Racejack-Shape"

DETAIL_UNAUTHORIZED: Final = "unauthorized"
DETAIL_NOT_FOUND: Final = "not found"
DETAIL_REFUSED: Final = "request could not be completed"
DETAIL_BAD_REQUEST: Final = "bad request"


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=DETAIL_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bad_request() -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=DETAIL_BAD_REQUEST)


def not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=DETAIL_NOT_FOUND)


def refused() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail=DETAIL_REFUSED)


def require_buyer(authorization: str | None) -> str:
    buyer_id = authenticate(authorization)
    if buyer_id is None:
        raise unauthorized()
    return buyer_id


def pool_of(request: Request) -> ConnPool:
    pool: ConnPool = request.app.state.pool
    return pool


def stamp_requests(app: FastAPI, replica_name: str) -> None:
    """Give every request a correlation id and every response the replica that served it."""

    @app.middleware("http")
    async def stamp(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[REPLICA_HEADER] = replica_name
        return response


def add_common_routes(app: FastAPI, settings: AppConfig, *, variant: str, strategy: str) -> None:
    """Health and the store's own read views — byte-identical across variants."""

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        async with pool_of(request).connection() as conn:
            await conn.execute("SELECT 1")
        return HealthResponse(
            status="ok", replica=settings.replica_name, variant=variant, strategy=strategy
        )

    @app.get("/drops/{drop_id}", response_model=DropView)
    async def get_drop(
        drop_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> DropView:
        require_buyer(authorization)
        async with pool_of(request).connection() as conn:
            view = await read_drop(conn, drop_id)
        if view is None:
            raise not_found()
        return view

    @app.get("/credit/wallets/{wallet_id}", response_model=WalletView)
    async def get_wallet(
        wallet_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WalletView:
        require_buyer(authorization)
        async with pool_of(request).connection() as conn:
            view = await read_wallet(conn, wallet_id)
        if view is None:
            raise not_found()
        return view


def finish_order(
    result: OrderResult, *, drop_id: str, buyer_id: str, request_id: str, replica: str
) -> OrderResponse:
    """Turn a write-side outcome into the client's answer. Shared, so the answers cannot diverge."""
    if result.outcome is OrderOutcome.UNKNOWN_DROP:
        raise not_found()
    if result.outcome is OrderOutcome.REFUSED:
        audit.emit_refusal(
            request_id=request_id,
            replica=replica,
            operation=audit.RefusedOperation.PLACE_ORDER,
            resource_type="drop",
            resource_id=drop_id,
        )
        raise refused()
    # Narrowed by the CONFIRMED outcome; no write path returns one without the other.
    assert result.order_id is not None
    return OrderResponse(
        order_id=result.order_id,
        drop_id=drop_id,
        buyer_id=buyer_id,
        units=1,
        status=OrderStatus.CONFIRMED,
    )


def finish_redemption(
    result: RedemptionResult,
    *,
    code: str,
    wallet_id: str,
    buyer_id: str,
    request_id: str,
    replica: str,
) -> RedemptionResponse:
    if result.outcome is RedemptionOutcome.UNKNOWN_TARGET:
        raise not_found()
    if result.outcome is RedemptionOutcome.REFUSED:
        audit.emit_refusal(
            request_id=request_id,
            replica=replica,
            operation=audit.RefusedOperation.REDEEM_CREDIT_CODE,
            resource_type="credit_code",
            resource_id=code,
        )
        raise refused()
    # Narrowed by the CREDITED outcome.
    assert result.redemption_id is not None
    assert result.amount_cents is not None
    assert result.wallet_balance_cents is not None
    return RedemptionResponse(
        redemption_id=result.redemption_id,
        code=code,
        wallet_id=wallet_id,
        buyer_id=buyer_id,
        amount_cents=result.amount_cents,
        wallet_balance_cents=result.wallet_balance_cents,
        status=RedemptionStatus.CREDITED,
    )
