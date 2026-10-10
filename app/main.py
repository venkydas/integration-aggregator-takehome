import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import Deps, router
from app.bao import BaoClient, BaoError, NotFound
from app.config import Settings
from app.models import PendingConnect, TokenRequest
from app.store import TTLStore
from app.worker import TokenWorker

log = logging.getLogger("app")
access_log = logging.getLogger("app.access")


def create_app(settings: Settings | None = None, bao: BaoClient | None = None) -> FastAPI:
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = bao or BaoClient(settings)
        states: TTLStore[PendingConnect] = TTLStore(settings.state_ttl_seconds)
        requests: TTLStore[TokenRequest] = TTLStore(settings.request_ttl_seconds)
        worker = TokenWorker(client, requests, settings.workers, settings.queue_size)
        app.state.deps = Deps(settings, client, states, requests, worker)

        worker.start()
        janitor = asyncio.create_task(_purge_forever(states, requests))
        try:
            yield
        finally:
            janitor.cancel()
            await worker.stop()
            await client.aclose()

    app = FastAPI(title="Integration Aggregator", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        # Our own access log: path only. The default one would log the query
        # string, which for /callback contains the OAuth code and state.
        start = time.perf_counter()
        response = await call_next(request)
        access_log.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - start) * 1000,
        )
        return response

    @app.exception_handler(BaoError)
    async def bao_error(_: Request, exc: BaoError) -> JSONResponse:
        if isinstance(exc, NotFound):
            return JSONResponse({"detail": "not found"}, status_code=404)
        if 400 <= exc.status < 500:
            # Bad input the plugin rejected (invalid code, bad provider options).
            return JSONResponse({"detail": exc.errors or ["rejected by OpenBao"]}, status_code=400)
        log.error("OpenBao error: %s", exc)
        return JSONResponse({"detail": "OpenBao error"}, status_code=502)

    @app.exception_handler(httpx.HTTPError)
    async def bao_unreachable(_: Request, exc: httpx.HTTPError) -> JSONResponse:
        log.error("OpenBao unreachable: %s", type(exc).__name__)
        return JSONResponse({"detail": "OpenBao unavailable"}, status_code=503)

    return app


async def _purge_forever(*stores: TTLStore, interval: float = 30.0) -> None:
    while True:
        await asyncio.sleep(interval)
        for store in stores:
            store.purge()


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every OpenBao URL at INFO; our access log is enough.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, access_log=False)  # noqa: S104


if __name__ == "__main__":
    run()
