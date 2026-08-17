"""The secure application.

Both replicas run this module. Every response is deliberately uninformative about *why* it was
refused: "sold out" and "lost the race to another buyer" are the same 409 with the same body, and a
missing, malformed, unknown, or expired credential is the same 401 with the same body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from .. import audit
from ..auth import authenticate
from ..config import AppConfig, CounterGuard, parse_counter_guard
from ..db import ConnPool, make_pool
from ..models import (
    DropView,
    HealthResponse,
    OrderOutcome,
    OrderResponse,
    OrderStatus,
    RedemptionOutcome,
    RedemptionRequest,
    RedemptionResponse,
    RedemptionStatus,
    WalletView,
)
from ..store import read_drop, read_wallet
from .guards import COUNTER_STRATEGIES, redeem_credit_code

REQUEST_ID_HEADER: Final = "X-Request-Id"
REPLICA_HEADER: Final = "X-Racejack-Replica"
GUARD_HEADER: Final = "X-Racejack-Guard"

DETAIL_UNAUTHORIZED: Final = "unauthorized"
DETAIL_NOT_FOUND: Final = "not found"
DETAIL_REFUSED: Final = "request could not be completed"
DETAIL_BAD_REQUEST: Final = "bad request"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=DETAIL_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    settings = config or AppConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = make_pool(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )
        await pool.open(wait=True, timeout=60)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(
        title=f"racejack — secure storefront ({settings.replica_name})",
        summary=(
            "Fictional limited-release storefront whose two concurrency-sensitive invariants are "
            "held by atomic operations. Local educational material only."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = settings

    def pool_of(request: Request) -> ConnPool:
        pool: ConnPool = request.app.state.pool
        return pool

    @app.middleware("http")
    async def stamp_request(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid4())
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[REPLICA_HEADER] = settings.replica_name
        return response

    def require_buyer(authorization: str | None) -> str:
        buyer_id = authenticate(authorization)
        if buyer_id is None:
            raise _unauthorized()
        return buyer_id

    def selected_guard(raw: str | None) -> CounterGuard:
        guard = parse_counter_guard(raw, default=settings.default_counter_guard)
        if guard is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DETAIL_BAD_REQUEST)
        return guard

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(request: Request) -> HealthResponse:
        async with pool_of(request).connection() as conn:
            await conn.execute("SELECT 1")
        return HealthResponse(
            status="ok",
            replica=settings.replica_name,
            counter_guard=settings.default_counter_guard.value,
        )

    @app.post(
        "/drops/{drop_id}/orders",
        response_model=OrderResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def place_order(
        drop_id: str,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
        x_racejack_guard: Annotated[str | None, Header()] = None,
    ) -> OrderResponse:
        buyer_id = require_buyer(authorization)
        guard = selected_guard(x_racejack_guard)
        response.headers[GUARD_HEADER] = guard.value
        async with pool_of(request).connection() as conn:
            result = await COUNTER_STRATEGIES[guard](
                conn,
                drop_id=drop_id,
                buyer_id=buyer_id,
                served_by=settings.replica_name,
            )
        if result.outcome is OrderOutcome.UNKNOWN_DROP:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=DETAIL_NOT_FOUND)
        if result.outcome is OrderOutcome.REFUSED:
            audit.emit_refusal(
                request_id=request.state.request_id,
                replica=settings.replica_name,
                operation=audit.RefusedOperation.PLACE_ORDER,
                resource_type="drop",
                resource_id=drop_id,
            )
            raise HTTPException(status.HTTP_409_CONFLICT, detail=DETAIL_REFUSED)
        # Narrowed by the CONFIRMED outcome; the guards never return one without the other.
        assert result.order_id is not None
        return OrderResponse(
            order_id=result.order_id,
            drop_id=drop_id,
            buyer_id=buyer_id,
            units=1,
            status=OrderStatus.CONFIRMED,
        )

    @app.post(
        "/credit/redemptions",
        response_model=RedemptionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def redeem(
        payload: RedemptionRequest,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> RedemptionResponse:
        buyer_id = require_buyer(authorization)
        async with pool_of(request).connection() as conn:
            result = await redeem_credit_code(
                conn,
                code=payload.code,
                wallet_id=payload.wallet_id,
                buyer_id=buyer_id,
                served_by=settings.replica_name,
            )
        if result.outcome is RedemptionOutcome.UNKNOWN_TARGET:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=DETAIL_NOT_FOUND)
        if result.outcome is RedemptionOutcome.REFUSED:
            audit.emit_refusal(
                request_id=request.state.request_id,
                replica=settings.replica_name,
                operation=audit.RefusedOperation.REDEEM_CREDIT_CODE,
                resource_type="credit_code",
                resource_id=payload.code,
            )
            raise HTTPException(status.HTTP_409_CONFLICT, detail=DETAIL_REFUSED)
        # Narrowed by the CREDITED outcome.
        assert result.redemption_id is not None
        assert result.amount_cents is not None
        assert result.wallet_balance_cents is not None
        return RedemptionResponse(
            redemption_id=result.redemption_id,
            code=payload.code,
            wallet_id=payload.wallet_id,
            buyer_id=buyer_id,
            amount_cents=result.amount_cents,
            wallet_balance_cents=result.wallet_balance_cents,
            status=RedemptionStatus.CREDITED,
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=DETAIL_NOT_FOUND)
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=DETAIL_NOT_FOUND)
        return view

    @app.exception_handler(psycopg.OperationalError)
    async def on_database_unavailable(request: Request, exc: Exception) -> Response:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="service unavailable"
        ) from exc

    return app


app = create_app()
