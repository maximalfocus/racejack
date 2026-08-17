"""The vulnerable application. **Local educational material — never deploy this.**

Starting it takes two deliberate actions and refuses on either one alone: the opt-in Compose profile
selects the service, and ``ALLOW_VULNERABLE_DEMO=true`` acknowledges what it is. Neither the default
Compose path nor the verification boundary starts it by accident.

Its routes are byte-for-byte the secure application's, because they are literally the same code in
`racejack.api`. What differs is in `shapes.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from ..api import (
    SHAPE_HEADER,
    add_common_routes,
    bad_request,
    finish_order,
    finish_redemption,
    pool_of,
    require_buyer,
    stamp_requests,
)
from ..config import AppConfig, VulnerableShape, parse_vulnerable_shape
from ..db import ConnPool, make_pool
from ..instrumentation import Instrumentation
from ..models import OrderResponse, RedemptionRequest, RedemptionResponse
from .acknowledgement import require_acknowledgement
from .shapes import COUNTER_SHAPES, redeem_unguarded

VARIANT = "vulnerable"

INSTRUMENTATION_HEADER: Final = "X-Racejack-Instrumented-Window"
INSTRUMENTATION_ON: Final = "hold"
"""Ask this request to hold its time-of-check to time-of-use window open. Deterministic mode only.

Absent — which is the default — nothing runs: no query, no wait, no recorded step. That single
skipped branch is the entire footprint of the instrumentation in the natural reproduction mode.
"""


def instrumentation_pool_of(request: Request) -> ConnPool:
    """A connection of its own, outside the request's transaction — see `instrumentation.py`."""
    pool: ConnPool = request.app.state.instrumentation_pool
    return pool


def create_app(config: AppConfig | None = None) -> FastAPI:
    require_acknowledgement()
    settings = config or AppConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = make_pool(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )
        instrumentation_pool = make_pool(
            settings.database_url, min_size=1, max_size=settings.pool_max_size
        )
        await pool.open(wait=True, timeout=60)
        await instrumentation_pool.open(wait=True, timeout=60)
        app.state.pool = pool
        app.state.instrumentation_pool = instrumentation_pool
        try:
            yield
        finally:
            await pool.close()
            await instrumentation_pool.close()

    app = FastAPI(
        title=f"racejack — INTENTIONALLY VULNERABLE storefront ({settings.replica_name})",
        summary=(
            "Deliberately broken educational material: the check and the act are separate steps. "
            "Local only. Never deploy this."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = settings
    stamp_requests(app, settings.replica_name)
    add_common_routes(app, settings, variant=VARIANT, strategy=VulnerableShape.UNGUARDED.value)

    def instrumented(raw: str | None) -> bool:
        return (raw or "").strip().lower() == INSTRUMENTATION_ON

    def selected_shape(raw: str | None) -> VulnerableShape:
        shape = parse_vulnerable_shape(raw)
        if shape is None:
            raise bad_request()
        return shape

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
        x_racejack_instrumented_window: Annotated[str | None, Header()] = None,
        x_racejack_shape: Annotated[str | None, Header()] = None,
    ) -> OrderResponse:
        buyer_id = require_buyer(authorization)
        hold = instrumented(x_racejack_instrumented_window)
        shape = selected_shape(x_racejack_shape)
        response.headers[SHAPE_HEADER] = shape.value
        async with (
            pool_of(request).connection() as conn,
            instrumentation_pool_of(request).connection() as instrumentation_conn,
        ):
            result = await COUNTER_SHAPES[shape](
                conn,
                Instrumentation(instrumentation_conn, replica=settings.replica_name, enabled=hold),
                drop_id=drop_id,
                buyer_id=buyer_id,
                served_by=settings.replica_name,
                request_id=request.state.request_id,
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
        x_racejack_instrumented_window: Annotated[str | None, Header()] = None,
    ) -> RedemptionResponse:
        buyer_id = require_buyer(authorization)
        hold = instrumented(x_racejack_instrumented_window)
        async with (
            pool_of(request).connection() as conn,
            instrumentation_pool_of(request).connection() as instrumentation_conn,
        ):
            result = await redeem_unguarded(
                conn,
                Instrumentation(instrumentation_conn, replica=settings.replica_name, enabled=hold),
                code=payload.code,
                wallet_id=payload.wallet_id,
                buyer_id=buyer_id,
                served_by=settings.replica_name,
                request_id=request.state.request_id,
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
