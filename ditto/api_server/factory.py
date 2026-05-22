"""Wire up the FastAPI application.

:func:`create_api_server` is the single entry point used by
:mod:`ditto.api_server.__main__` at process start and by every test
fixture that needs a real app. Per-test instantiation (no module-level
``app =`` global) lets ``dependency_overrides`` stay isolated.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

import ditto
from ditto.api_server.config import (
    ApiServerConfig,
    parse_api_server_config_from_env,
)
from ditto.api_server.endpoints import health_router, metrics_router
from ditto.api_server.errors import ApiServerLifespanError
from ditto.api_server.middleware import (
    AuthPassThroughMiddleware,
    RequestIDMiddleware,
    register_exception_handlers,
)
from ditto.chain import create_chain_client
from ditto.db import create_db_engine, create_session_maker

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def create_api_server(config: ApiServerConfig | None = None) -> FastAPI:
    """Build a FastAPI app with lifespan, middleware, handlers, and routers.

    Args:
        config: Resolved config. When ``None``, falls back to
            :func:`parse_api_server_config_from_env` with ``commit_hash``
            set to ``"unknown"`` (useful for tests that do not exercise
            the git-rev lookup path).

    Returns:
        A :class:`fastapi.FastAPI` instance ready for ``uvicorn.run`` or
        :class:`httpx.AsyncClient`-driven testing.
    """
    if config is None:
        config = parse_api_server_config_from_env(commit_hash="unknown")

    app = FastAPI(
        title="Ditto API",
        version=ditto.__version__,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=_make_lifespan(),
    )
    # State the lifespan reads on entry. Setting these BEFORE lifespan
    # runs means a startup failure can still reference the config.
    app.state.config = config
    app.state.commit_hash = config.commit_hash

    # Order matters: RequestIDMiddleware is outermost so every other
    # middleware + handler sees the request id. Auth pass-through is a
    # placeholder for future hotkey-session checking.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(AuthPassThroughMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)

    return app


def _make_lifespan():
    """Build the lifespan async-context-manager bound to ``app.state.config``.

    Separated from :func:`create_api_server` so the function body stays
    flat and the lifespan logic is easier to test in isolation.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config: ApiServerConfig = app.state.config
        async with AsyncExitStack() as stack:
            try:
                engine = create_db_engine(config.postgres)
                stack.push_async_callback(engine.dispose)
                app.state.engine = engine
                app.state.session_maker = create_session_maker(engine)

                chain = await stack.enter_async_context(
                    create_chain_client(config.chain)
                )
                app.state.chain = chain
            except Exception as e:
                raise ApiServerLifespanError(
                    f"failed to open dependencies during startup: {e}"
                ) from e

            logger.info(
                f"api server lifespan ready: host={config.host} port={config.port} "
                f"commit={config.commit_hash}"
            )
            yield

    return lifespan
