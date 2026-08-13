"""FastAPI app factory.

Per-test instantiation (no module-level ``app =`` global) keeps
``dependency_overrides`` isolated across tests.
"""

from __future__ import annotations

import html
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

import ditto
from ditto.api_server.burn_settings import BurnSettingsResolver
from ditto.api_server.config import (
    ApiServerConfig,
    parse_api_server_config_from_env,
)
from ditto.api_server.continual_retest_settings import ContinualRetestSettingsResolver
from ditto.api_server.datapipeline import create_generator
from ditto.api_server.efficiency_settings import (
    DEFAULT_SETTINGS_TTL_SECONDS,
    EfficiencyBonusSettingsResolver,
)
from ditto.api_server.embedding import create_embedder
from ditto.api_server.endpoints import (
    admin_artifact_release_settings_router,
    admin_attestation_router,
    admin_benchmark_rollout_router,
    admin_burn_settings_router,
    admin_confirmation_bundles_router,
    admin_continual_retest_settings_router,
    admin_copy_review_router,
    admin_efficiency_bonus_settings_router,
    admin_inference_concurrency_settings_router,
    admin_inference_routes_router,
    admin_lease_revocations_router,
    admin_miner_fees_router,
    admin_owner_router,
    admin_quarantine_router,
    admin_queue_policy_settings_router,
    admin_retirement_router,
    admin_scoring_readiness_router,
    admin_screener_capacity_router,
    admin_screener_review_settings_router,
    admin_submission_deposit_address_router,
    admin_submission_settings_router,
    admin_validation_retry_router,
    admin_validator_slot_settings_router,
    attestation_router,
    health_router,
    inference_router,
    metrics_router,
    public_router,
    retrieval_router,
    scoring_router,
    screener_router,
    upload_router,
    validator_confirmation_router,
    validator_router,
)
from ditto.api_server.errors import ApiServerConfigError, ApiServerLifespanError
from ditto.api_server.inference_concurrency_settings import (
    InferenceConcurrencySettingsResolver,
)
from ditto.api_server.inference_routing import ProviderRouteRefresher
from ditto.api_server.middleware import (
    AuthPassThroughMiddleware,
    PublicCacheMiddleware,
    RequestIDMiddleware,
    SizedGZipMiddleware,
    register_exception_handlers,
)
from ditto.api_server.middleware.public_cache import compute_etag, if_none_match
from ditto.api_server.payment_verifier import create_payment_verifier
from ditto.api_server.pricing import create_price_oracle
from ditto.api_server.queue_policy_settings import QueuePolicySettingsResolver
from ditto.api_server.storage import create_storage_client
from ditto.api_server.validator_names import create_validator_names
from ditto.api_server.validator_nonce_janitor import ValidatorNonceJanitor
from ditto.api_server.validator_slot_settings import ValidatorSlotSettingsResolver
from ditto.chain import create_chain_client
from ditto.db import create_db_engine, create_session_maker

logger = logging.getLogger(__name__)

# The dashboard SPA lives at the repo root (source checkout on the deployed VM)
# and is built with Vite (`npm run build` in dashboard/, done by update.sh at
# deploy time). It is not packaged into the wheel, so a missing build just
# disables the route.
_DASHBOARD_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"
_DASHBOARD_FILE = _DASHBOARD_DIST / "index.html"
_DASHBOARD_ASSETS = _DASHBOARD_DIST / "assets"
_WANDB_META_RE = re.compile(r'(<meta name="ditto:wandb-url" content=")[^"]*(")')


PLATFORM_ROLE = "platform"
RELAY_ROLE = "relay"


def _process_role() -> str:
    """Which subset of the API this process serves (``DITTO_ROLE``).

    The inference proxy and validator heartbeat ingest share one event loop in
    a single fork-mode uvicorn process. Proxy load therefore delays heartbeat
    ingest, which lets ``seen_at``/``benchmark_capacity`` go stale, which makes
    the platform force-expire live validator leases and destroy healthy
    in-flight benchmark runs. The proxy can kill the runs it is serving.

    ``relay`` mounts only ``/health``, ``/metrics``, and the inference plane, so
    it can be deployed as a separate process on its own host. That severs the
    shared loop -- which is the actual causal mechanism -- while keeping one
    codebase, one set of queries, one schema, and one migration history. There
    is no second implementation to keep byte-identical, because there is no
    second implementation.

    Unknown values fail boot rather than defaulting. Silently falling back to
    ``platform`` would mean a typo in the relay's environment quietly serves the
    full admin and upload surface from a host provisioned to be a proxy.
    """
    role = os.environ.get("DITTO_ROLE", PLATFORM_ROLE).strip().lower() or PLATFORM_ROLE
    if role not in {PLATFORM_ROLE, RELAY_ROLE}:
        raise ApiServerConfigError(
            f"DITTO_ROLE must be {PLATFORM_ROLE!r} or {RELAY_ROLE!r}, got {role!r}"
        )
    return role


def _efficiency_settings_ttl_seconds() -> float:
    """TTL for the hot-swappable efficiency-bonus policy read cache.

    ``DITTO_EFFICIENCY_BONUS_SETTINGS_TTL_SECONDS`` overrides the default; a
    malformed or negative value falls back to the default rather than failing
    boot (the setting only bounds propagation latency, never correctness).
    """
    raw = os.environ.get("DITTO_EFFICIENCY_BONUS_SETTINGS_TTL_SECONDS")
    if raw is None:
        return DEFAULT_SETTINGS_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SETTINGS_TTL_SECONDS
    return value if value >= 0 else DEFAULT_SETTINGS_TTL_SECONDS


def _render_dashboard(wandb_url: str) -> str | None:
    """Read the dashboard SPA and inject the public wandb project URL.

    Returns ``None`` (route is skipped) when the file is absent — e.g. a
    packaged/wheel install or a checkout where ``dashboard/dist`` was never
    built (``npm run build``). The ``api-base`` meta is left empty on purpose
    so the SPA falls back to its same-origin ``/api/v1`` default when the
    platform serves it.
    """
    try:
        page = _DASHBOARD_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    return _WANDB_META_RE.sub(
        lambda m: m.group(1) + html.escape(wandb_url, quote=True) + m.group(2),
        page,
        count=1,
    )


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
                send_address=config.upload_payment_address,
            )
            app.state.payment_verifier = payment_verifier

            storage = await stack.enter_async_context(
                create_storage_client(config.storage)
            )
            app.state.storage = storage

            embedder = create_embedder(config.embedding)
            stack.push_async_callback(embedder.aclose)
            app.state.embedder = embedder

            generator = create_generator(config.data_pipeline)
            stack.push_async_callback(generator.aclose)
            app.state.dataset_generator = generator

            inference_client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.inference_proxy.timeout_seconds),
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=config.inference_proxy.global_concurrency,
                    max_keepalive_connections=config.inference_proxy.global_concurrency,
                ),
            )
            stack.push_async_callback(inference_client.aclose)
            app.state.inference_client = inference_client
            provider_routes = ProviderRouteRefresher(
                config=config.inference_proxy,
                session_maker=app.state.session_maker,
                client=inference_client,
            )
            stack.push_async_callback(provider_routes.aclose)
            # Exactly ONE process may run discovery. The refresh loop upserts
            # InferenceRoutingPolicy and InferenceProviderRoute rows by hand --
            # `session.get` then `session.add` -- with no ON CONFLICT, no row
            # lock and no savepoint, all inside a single transaction per model.
            # Two copies racing it collide on the primary key, and because the
            # insert shares that transaction the whole model's refresh cycle is
            # discarded; the loop's handler logs only the exception type, so it
            # degrades silently. The unlocked read-modify-write on `status` can
            # also flap a route between discovered and offline, which feeds
            # selection's status filter.
            #
            # The relay does not need it: route selection reads Postgres per
            # request under `FOR UPDATE`, so there is no in-memory route cache
            # to keep warm. This makes the platform role the designated primary
            # for the one piece of background work that requires a singleton.
            if _process_role() == PLATFORM_ROLE:
                await provider_routes.start()
            app.state.inference_provider_routes = provider_routes

            nonce_janitor = ValidatorNonceJanitor(session_maker=app.state.session_maker)
            stack.push_async_callback(nonce_janitor.aclose)
            if _process_role() == PLATFORM_ROLE:
                await nonce_janitor.start()
            app.state.validator_nonce_janitor = nonce_janitor

            validator_names = app.state.validator_names
            stack.push_async_callback(validator_names.aclose)
            await validator_names.start()
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
    # Hot-swappable efficiency-bonus policy: the compute path resolves the
    # latest append-only revision through this resolver (short TTL), falling
    # back to the env seed (config.efficiency_bonus) when none exists. The DB
    # read uses app.state.session_maker (set in lifespan), so before lifespan —
    # or in unit tests without it — resolve() just returns the seed.
    app.state.efficiency_settings = EfficiencyBonusSettingsResolver(
        config.efficiency_bonus,
        ttl_seconds=_efficiency_settings_ttl_seconds(),
    )
    # Operator-owned share of miner emission routed to the owner burn hotkey.
    # Served on the scoring ledger, so a change reaches the fleet on its next
    # poll instead of on a validator release.
    app.state.burn_settings = BurnSettingsResolver()
    app.state.continual_retest_settings = ContinualRetestSettingsResolver()
    app.state.queue_policy_settings = QueuePolicySettingsResolver()
    app.state.inference_concurrency_settings = InferenceConcurrencySettingsResolver()
    # Operator cap on concurrent benchmark slots per validator. Read at ticket
    # issue time; falls back to its conservative module default (cap 2) whenever
    # a revision cannot be read, so no failure path uncaps the fleet.
    app.state.validator_slot_settings = ValidatorSlotSettingsResolver()
    # The object exists even when lifespan is skipped in unit tests. Its
    # snapshot path is synchronous and disabled by default; production lifespan
    # starts the optional background refresher without blocking API startup.
    app.state.validator_names = create_validator_names(config.validator_names)

    # Starlette inserts each middleware at position 0, so the LAST
    # add_middleware call ends up outermost on the wire. RequestIDMiddleware
    # must be outermost so its contextvar is live for every downstream
    # middleware + handler + log line, including any future auth that
    # short-circuits before reaching the app.
    # Gzip is inside the public cache: each identity/gzip representation is
    # built once and cached independently, so a 200KB operations cache HIT
    # does not burn CPU recompressing the same user-agnostic bytes.
    app.add_middleware(SizedGZipMiddleware, minimum_size=1000, compresslevel=6)
    # PublicCacheMiddleware wraps gzip. Cache hits skip both endpoint/DB work
    # and compression while request-id logging still records every request.
    app.add_middleware(PublicCacheMiddleware)
    app.add_middleware(AuthPassThroughMiddleware)
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    if _process_role() == RELAY_ROLE:
        # The inference plane only. Nothing below this point is mounted, so a
        # relay host cannot serve uploads, scoring, the validator API, or any
        # admin surface even if it is reachable and holds valid credentials.
        # /health and /metrics stay so the load balancer and Prometheus scrape
        # the same paths on both roles.
        app.include_router(inference_router, prefix="/api/v1")
        return app
    app.include_router(attestation_router, prefix="/api/v1")
    app.include_router(upload_router, prefix="/api/v1")
    app.include_router(retrieval_router, prefix="/api/v1")
    app.include_router(validator_router, prefix="/api/v1")
    app.include_router(validator_confirmation_router, prefix="/api/v1")
    app.include_router(inference_router, prefix="/api/v1")
    app.include_router(screener_router, prefix="/api/v1")
    app.include_router(scoring_router, prefix="/api/v1")
    app.include_router(public_router, prefix="/api/v1")
    app.include_router(admin_artifact_release_settings_router, prefix="/api/v1")
    app.include_router(admin_attestation_router, prefix="/api/v1")
    app.include_router(admin_benchmark_rollout_router, prefix="/api/v1")
    app.include_router(admin_queue_policy_settings_router, prefix="/api/v1")
    app.include_router(admin_inference_concurrency_settings_router, prefix="/api/v1")
    app.include_router(admin_efficiency_bonus_settings_router, prefix="/api/v1")
    app.include_router(admin_inference_routes_router, prefix="/api/v1")
    app.include_router(admin_lease_revocations_router, prefix="/api/v1")
    app.include_router(admin_owner_router, prefix="/api/v1")
    app.include_router(admin_quarantine_router, prefix="/api/v1")
    app.include_router(admin_validation_retry_router, prefix="/api/v1")
    app.include_router(admin_retirement_router, prefix="/api/v1")
    app.include_router(admin_validator_slot_settings_router, prefix="/api/v1")
    app.include_router(admin_scoring_readiness_router, prefix="/api/v1")
    app.include_router(admin_screener_review_settings_router, prefix="/api/v1")
    app.include_router(admin_screener_capacity_router, prefix="/api/v1")
    app.include_router(admin_submission_settings_router, prefix="/api/v1")
    app.include_router(admin_submission_deposit_address_router, prefix="/api/v1")
    app.include_router(admin_copy_review_router, prefix="/api/v1")
    app.include_router(admin_confirmation_bundles_router, prefix="/api/v1")
    app.include_router(admin_continual_retest_settings_router, prefix="/api/v1")
    app.include_router(admin_burn_settings_router, prefix="/api/v1")
    app.include_router(admin_miner_fees_router, prefix="/api/v1")

    # Serve the public dashboard SPA same-origin at ``/`` so the platform is the
    # transparency front door (its ``/api/v1/public/*`` calls need no CORS). The
    # HTML is rendered once at boot with the wandb link injected; a missing file
    # just skips the route.
    if config.dashboard_enabled:
        dashboard_html = _render_dashboard(config.dashboard_wandb_url)
        if dashboard_html is None:
            logger.info(
                "dashboard SPA not found at %s; serving API only", _DASHBOARD_FILE
            )
        else:
            # The HTML is rendered once at boot, so hash it once for a strong
            # ETag. With max-age=60 the SPA revalidates often, but a matching
            # If-None-Match returns an empty 304 — cheap for the VM and the
            # main win of the polling dashboard. must-revalidate + the short
            # TTL keeps deploys visible within a minute.
            dashboard_etag = compute_etag(dashboard_html.encode("utf-8"))
            dashboard_headers = {
                "Cache-Control": "public, max-age=60, must-revalidate",
                "ETag": dashboard_etag,
            }

            async def dashboard_response(request: Request) -> Response:
                if if_none_match(request.headers.get("if-none-match"), dashboard_etag):
                    # The 200 this revalidates carries "Vary: Accept-Encoding"
                    # (added by the gzip layer); RFC 9110 §15.4.5 says the 304
                    # must repeat it. Set here only — on the shared 200 headers
                    # gzip's add_vary_header would append a duplicate.
                    return Response(
                        status_code=304,
                        headers={**dashboard_headers, "Vary": "Accept-Encoding"},
                    )
                return HTMLResponse(content=dashboard_html, headers=dashboard_headers)

            @app.get("/", include_in_schema=False, response_class=HTMLResponse)
            async def dashboard(request: Request) -> Response:
                return await dashboard_response(request)

            # Backward-compatible plural URLs serve the SPA shell and normalize to
            # query-param popovers. Singular agent/miner URLs are dedicated views.
            for entity_kind in ("agents", "miners", "validators", "screeners"):
                app.add_api_route(
                    f"/{entity_kind}/{{entity_id}}",
                    dashboard_response,
                    methods=["GET"],
                    include_in_schema=False,
                    response_class=HTMLResponse,
                    name=f"dashboard_{entity_kind}",
                )
            for entity_kind in ("agent", "miner"):
                app.add_api_route(
                    f"/{entity_kind}/{{entity_id}}",
                    dashboard_response,
                    methods=["GET"],
                    include_in_schema=False,
                    response_class=HTMLResponse,
                    name=f"dashboard_{entity_kind}",
                )

            if _DASHBOARD_ASSETS.is_dir():
                # Vite emits content-hashed bundles under dist/assets/ (safe to
                # cache forever); files copied through from public/assets/ (the
                # og:image PNG) keep their names, so they get a day, not a year.
                _hashed = re.compile(r"-[A-Za-z0-9_-]{8,}\.[a-z0-9]+$")

                @app.get(
                    "/assets/{asset_path:path}",
                    include_in_schema=False,
                    response_class=FileResponse,
                )
                async def dashboard_asset(asset_path: str) -> FileResponse:
                    resolved = (_DASHBOARD_ASSETS / asset_path).resolve()
                    if not (
                        resolved.is_relative_to(_DASHBOARD_ASSETS.resolve())
                        and resolved.is_file()
                    ):
                        raise HTTPException(status_code=404)
                    cache = (
                        "public, max-age=31536000, immutable"
                        if _hashed.search(resolved.name)
                        else "public, max-age=86400"
                    )
                    return FileResponse(resolved, headers={"Cache-Control": cache})

    return app
