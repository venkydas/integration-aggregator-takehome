"""Thin async client for the parts of OpenBao the aggregator uses.

All OAuth work (code exchange, token storage, refresh) happens inside the
oauthapp plugin; this module only forwards requests to it.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class BaoError(Exception):
    def __init__(self, status: int, errors: list[str]):
        super().__init__(f"OpenBao returned {status}: {'; '.join(errors) or 'no detail'}")
        self.status = status
        self.errors = errors


class NotFound(BaoError):
    pass


@dataclass(frozen=True)
class Token:
    access_token: str
    token_type: str
    expire_time: str | None


class BaoClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.bao_addr,
            timeout=settings.bao_timeout_seconds,
            transport=transport,
        )
        self._static_token = settings.bao_token is not None
        self._token = settings.bao_token.get_secret_value() if settings.bao_token else None
        self._token_expires_at = float("inf") if self._static_token else 0.0
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- plugin operations ----------------------------------------------------

    async def put_server(
        self,
        name: str,
        provider: str,
        client_id: str,
        client_secret: str,
        provider_options: dict[str, str],
    ) -> None:
        await self._request(
            "PUT",
            f"servers/{name}",
            {
                "provider": provider,
                "client_id": client_id,
                "client_secret": client_secret,
                "provider_options": provider_options,
            },
        )

    async def get_server(self, name: str) -> dict[str, Any]:
        return (await self._request("GET", f"servers/{name}"))["data"]

    async def auth_code_url(
        self, server: str, redirect_url: str, state: str, scopes: list[str]
    ) -> str:
        body = {"server": server, "redirect_url": redirect_url, "state": state, "scopes": scopes}
        return (await self._request("PUT", "auth-code-url", body))["data"]["url"]

    async def exchange_code(self, cred: str, server: str, code: str, redirect_url: str) -> None:
        body = {"server": server, "code": code, "redirect_url": redirect_url}
        await self._request("PUT", f"creds/{cred}", body)

    async def get_token(self, cred: str) -> Token:
        data = (await self._request("GET", f"creds/{cred}"))["data"]
        return Token(
            access_token=data["access_token"],
            token_type=data.get("type", "Bearer"),
            expire_time=data.get("expire_time"),
        )

    async def healthy(self) -> bool:
        try:
            r = await self._http.get("/v1/sys/health")
        except httpx.HTTPError:
            return False
        # 200 only when initialised, unsealed and active.
        return r.status_code == 200

    # --- plumbing -------------------------------------------------------------

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        url = f"/v1/{self._settings.bao_mount}/{path}"
        r = await self._http.request(method, url, json=json, headers=await self._auth_header())
        if r.status_code == 403 and not self._static_token:
            # Our token was revoked or expired early: log in again, retry once.
            self._token = None
            r = await self._http.request(method, url, json=json, headers=await self._auth_header())
        return self._parse(r)

    async def _auth_header(self) -> dict[str, str]:
        return {"X-Vault-Token": await self._ensure_token()}

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._login_lock:
            # Another task may have logged in while we waited for the lock.
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            # Kubernetes rotates this file, so read it on every login.
            jwt = (await asyncio.to_thread(Path(self._settings.k8s_token_path).read_text)).strip()
            r = await self._http.post(
                f"/v1/auth/{self._settings.bao_k8s_mount}/login",
                json={"role": self._settings.bao_k8s_role, "jwt": jwt},
            )
            auth = self._parse(r)["auth"]
            self._token = auth["client_token"]
            # Renew well before expiry so in-flight calls never carry a dead token.
            self._token_expires_at = time.monotonic() + auth["lease_duration"] * 0.8
            log.info("logged in to OpenBao with Kubernetes auth")
            return self._token

    @staticmethod
    def _parse(r: httpx.Response) -> dict:
        if r.status_code == 204:
            return {}
        if r.is_success:
            return r.json()
        try:
            errors = [str(e) for e in r.json().get("errors", [])]
        except ValueError:
            errors = [r.text[:200]]
        cls = NotFound if r.status_code == 404 else BaoError
        raise cls(r.status_code, errors)
