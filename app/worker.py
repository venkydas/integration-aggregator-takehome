"""Background workers that fulfil token requests.

GET /{provider}/{user} only enqueues a request id and returns 202; these
workers fetch the token from OpenBao and record the result for polling.
"""

import asyncio
import logging

import httpx

from app.bao import BaoClient, BaoError, NotFound
from app.models import TokenRequest, cred_name
from app.store import TTLStore

log = logging.getLogger(__name__)


class TokenWorker:
    def __init__(
        self,
        bao: BaoClient,
        requests: TTLStore[TokenRequest],
        concurrency: int,
        queue_size: int,
    ):
        self._bao = bao
        self._requests = requests
        self._concurrency = concurrency
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(), name=f"token-worker-{i}")
            for i in range(self._concurrency)
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def submit(self, request_id: str) -> None:
        """Queue a request; raises asyncio.QueueFull when overloaded."""
        self._queue.put_nowait(request_id)

    async def _run(self) -> None:
        while True:
            request_id = await self._queue.get()
            try:
                await self._process(request_id)
            except Exception:
                # One bad request must never take a worker down with it, and
                # the caller must not be left polling a request stuck in pending.
                log.exception("unexpected error processing request %s", request_id)
                if req := self._requests.get(request_id):
                    req.fail("internal_error", "unexpected error, see service logs")
            finally:
                self._queue.task_done()

    async def _process(self, request_id: str) -> None:
        req = self._requests.get(request_id)
        if req is None:
            return  # expired before a worker got to it
        try:
            token = await self._bao.get_token(cred_name(req.provider, req.user))
        except NotFound:
            req.fail("not_connected", f"user '{req.user}' has not connected '{req.provider}'")
        except BaoError as e:
            # e.g. refresh failed or the provider was misconfigured.
            req.fail("token_unavailable", "; ".join(e.errors) or str(e))
        except httpx.HTTPError:
            log.warning("OpenBao unreachable while processing request %s", request_id)
            req.fail("openbao_unavailable", "could not reach OpenBao")
        else:
            req.complete(token)
