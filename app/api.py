import asyncio
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from app.bao import BaoClient, NotFound
from app.config import Settings
from app.models import (
    DEFAULT_SCOPES,
    PROVIDER_NAME,
    USER_ID,
    ConnectedOut,
    ConnectIn,
    ConnectOut,
    PendingConnect,
    ProviderIn,
    ProviderOut,
    RequestOut,
    TokenRequest,
    cred_name,
)
from app.store import TTLStore
from app.worker import TokenWorker


@dataclass
class Deps:
    settings: Settings
    bao: BaoClient
    states: TTLStore[PendingConnect]
    requests: TTLStore[TokenRequest]
    worker: TokenWorker

    @property
    def redirect_url(self) -> str:
        return f"{self.settings.public_url.rstrip('/')}/callback"


def get_deps(request: Request) -> Deps:
    return request.app.state.deps


D = Annotated[Deps, Depends(get_deps)]
ProviderPath = Annotated[str, Path(pattern=PROVIDER_NAME)]
UserPath = Annotated[str, Path(pattern=USER_ID)]

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(deps: D) -> dict:
    if not await deps.bao.healthy():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OpenBao not ready")
    return {"status": "ready"}


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def register_provider(body: ProviderIn, deps: D) -> ProviderOut:
    """Create or update a provider. The client secret goes straight to OpenBao."""
    await deps.bao.put_server(
        name=body.name,
        provider=body.provider,
        client_id=body.client_id,
        client_secret=body.client_secret.get_secret_value(),
        provider_options=body.provider_options,
    )
    return ProviderOut(name=body.name, provider=body.provider)


@router.post("/providers/{provider}/users/{user}/connect")
async def connect(
    provider: ProviderPath, user: UserPath, deps: D, body: ConnectIn | None = None
) -> ConnectOut:
    """Start the OAuth flow: returns the URL the user must open in a browser."""
    try:
        server = await deps.bao.get_server(provider)
    except NotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"provider '{provider}' not registered"
        ) from None

    scopes = body.scopes if body and body.scopes is not None else DEFAULT_SCOPES[server["provider"]]
    # `state` ties the provider's redirect back to this exact connect call and
    # protects /callback against CSRF. Single use, short lived.
    state = secrets.token_urlsafe(32)
    deps.states.put(state, PendingConnect(provider=provider, user=user))
    url = await deps.bao.auth_code_url(provider, deps.redirect_url, state, scopes)
    return ConnectOut(authorization_url=url, expires_in=deps.settings.state_ttl_seconds)


@router.get("/callback")
async def callback(
    deps: D, code: str | None = None, state: str | None = None, error: str | None = None
) -> ConnectedOut:
    """Where the provider redirects the browser. The plugin exchanges the code."""
    pending = deps.states.pop(state) if state else None
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"authorization failed: {error[:100]}")
    if pending is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown or expired state")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing code")

    await deps.bao.exchange_code(
        cred=cred_name(pending.provider, pending.user),
        server=pending.provider,
        code=code,
        redirect_url=deps.redirect_url,
    )
    return ConnectedOut(provider=pending.provider, user=pending.user)


# Must be registered before /{provider}/{user}, which would otherwise match it.
@router.get("/requests/{request_id}")
async def get_request(request_id: str, deps: D, response: Response) -> RequestOut:
    req = deps.requests.get(request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or expired request")
    if req.status == "pending":
        response.headers["Retry-After"] = "1"
    return RequestOut.from_request(req)


@router.get("/{provider}/{user}", status_code=status.HTTP_202_ACCEPTED)
async def request_token(
    provider: ProviderPath, user: UserPath, deps: D, response: Response
) -> RequestOut:
    """Ask for a user's current access token; poll the Location URL for it."""
    req = TokenRequest(id=secrets.token_urlsafe(16), provider=provider, user=user)
    deps.requests.put(req.id, req)
    try:
        deps.worker.submit(req.id)
    except asyncio.QueueFull:
        deps.requests.pop(req.id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "too many pending requests",
            headers={"Retry-After": "1"},
        ) from None
    response.headers["Location"] = f"/requests/{req.id}"
    return RequestOut.from_request(req)
