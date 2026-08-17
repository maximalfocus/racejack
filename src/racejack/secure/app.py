"""The secure application.

Both `app-a` and `app-b` run this module. Its whole content is the two write routes, because
everything else — credentials, read views, refusal responses, success payloads — is shared with the
vulnerable variant in `racejack.api`. What is left here is the part that matters: which guard runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from ..api import (
    GUARD_HEADER,
    add_common_routes,
    bad_request,
    finish_order,
    finish_redemption,
    pool_of,
    require_buyer,
    stamp_requests,
)
from ..config import AppConfig, CounterGuard, parse_counter_guard
from ..db import make_pool
from ..models import OrderResponse, RedemptionRequest, RedemptionResponse
from .guards import COUNTER_STRATEGIES, redeem_credit_code

VARIANT = "secure"


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
    stamp_requests(app, settings.replica_name)
    add_common_routes(app, settings, variant=VARIANT, strategy=settings.default_counter_guard.value)

    def selected_guard(raw: str | None) -> CounterGuard:
        guard = parse_counter_guard(raw, default=settings.default_counter_guard)
        if guard is None:
            raise bad_request()
        return guard

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
                conn, drop_id=drop_id, buyer_id=buyer_id, served_by=settings.replica_name
            )
        return finish_order(
            result,
            drop_id=drop_id,
            buyer_id=buyer_id,
            request_id=request.state.request_id,
            replica=settings.replica_name,
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
        return finish_redemption(
            result,
            code=payload.code,
            wallet_id=payload.wallet_id,
            buyer_id=buyer_id,
            request_id=request.state.request_id,
            replica=settings.replica_name,
        )

    @app.exception_handler(psycopg.OperationalError)
    async def on_database_unavailable(request: Request, exc: Exception) -> Response:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="service unavailable"
        ) from exc

    return app


app = create_app()
