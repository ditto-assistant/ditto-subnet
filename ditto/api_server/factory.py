"""FastAPI app factory.

Per-test instantiation (no module-level ``app =`` global) keeps
``dependency_overrides`` isolated across tests.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

import ditto
from ditto.api_server.config import (
    ApiServerConfig,
    parse_api_server_config_from_env,
)
from ditto.api_server.endpoints import (
    health_router,
    metrics_router,
    retrieval_router,
    upload_router,
)
from ditto.api_server.errors import ApiServerLifespanError
from ditto.api_server.middleware import (
    AuthPassThroughMiddleware,
    RequestIDMiddleware,
    register_exception_handlers,
)
from ditto.api_server.payment_verifier import create_payment_verifier
from ditto.api_server.pricing import create_price_oracle
from ditto.api_server.storage import create_storage_client
from ditto.chain import create_chain_client
from ditto.db import create_db_engine, create_session_maker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: ApiServerConfig = app.state.config
    async with AsyncExitStack() as stack:
        try:
            engine = create_db_engine(config.postgres)
            stack.push_async_callback(engine.dispose)
            app.state.engine = engine
            app.state.session_maker = create_session_maker(engine)

            chain = await stack.enter_async_context(create_chain_client(config.chain))
            app.state.chain = chain

            price_oracle = create_price_oracle(config.pricing)
            stack.push_async_callback(price_oracle.aclose)
            app.state.price_oracle = price_oracle

            payment_verifier = create_payment_verifier(
                chain=chain,
                oracle=price_oracle,
                pricing_config=config.pricing,
                send_address=config.upload_payment_address,
            )
            app.state.payment_verifier = payment_verifier

            storage = await stack.enter_async_context(
                create_storage_client(config.storage)
            )
            app.state.storage = storage
        except Exception as e:
            raise ApiServerLifespanError(
                f"failed to open dependencies during startup: {e}"
            ) from e

        logger.info(
            f"api server ready on {config.host}:{config.port} "
            f"commit={config.commit_hash}"
        )
        yield
        logger.info("api server shutting down")


def create_api_server(config: ApiServerConfig | None = None) -> FastAPI:
    """Build the FastAPI app, lifespan, middleware, handlers, and routers.

    When ``config`` is ``None``, falls back to
    :func:`parse_api_server_config_from_env` with ``commit_hash`` set to
    ``"unknown"`` so tests that do not exercise the git-rev path can
    skip resolving it.
    """
    if config is None:
        config = parse_api_server_config_from_env(commit_hash="unknown")

    app = FastAPI(
        title="Ditto API",
        version=ditto.__version__,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.config = config
    app.state.commit_hash = config.commit_hash

    # Starlette inserts each middleware at position 0, so the LAST
    # add_middleware call ends up outermost on the wire. RequestIDMiddleware
    # must be outermost so its contextvar is live for every downstream
    # middleware + handler + log line, including any future auth that
    # short-circuits before reaching the app.
    app.add_middleware(AuthPassThroughMiddleware)
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(upload_router, prefix="/api/v1")
    app.include_router(retrieval_router, prefix="/api/v1")

    return app
