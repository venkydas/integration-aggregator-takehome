from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.bao import Token

# Names end up inside OpenBao paths (servers/<provider>, creds/<provider>/<user>),
# so they are restricted to a safe alphabet: no '/', no '..' tricks.
PROVIDER_NAME = r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"
USER_ID = r"^[A-Za-z0-9](?:[A-Za-z0-9._@-]{0,126}[A-Za-z0-9])?$"

# First path segments that belong to our own routes, so GET /{provider}/{user}
# can never be shadowed by (or shadow) them.
RESERVED_NAMES = {"providers", "requests", "callback", "healthz", "readyz", "docs", "openapi"}

ProviderType = Literal["github", "gitlab", "google", "oidc", "custom"]

DEFAULT_SCOPES: dict[str, list[str]] = {
    "github": ["read:user"],
    "gitlab": ["read_user"],
    "google": ["openid", "email"],
    "oidc": ["openid"],
    "custom": [],
}


def cred_name(provider: str, user: str) -> str:
    return f"{provider}/{user}"


class ProviderIn(BaseModel):
    name: str = Field(pattern=PROVIDER_NAME, examples=["github"])
    provider: ProviderType
    client_id: str = Field(min_length=1)
    # SecretStr keeps the value out of logs, reprs and error messages.
    client_secret: SecretStr
    provider_options: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def not_reserved(cls, v: str) -> str:
        if v in RESERVED_NAMES:
            raise ValueError(f"'{v}' is reserved")
        return v


class ProviderOut(BaseModel):
    name: str
    provider: ProviderType


class ConnectIn(BaseModel):
    scopes: list[str] | None = None


class ConnectOut(BaseModel):
    authorization_url: str
    expires_in: int


class ConnectedOut(BaseModel):
    status: Literal["connected"] = "connected"
    provider: str
    user: str


@dataclass(frozen=True)
class PendingConnect:
    provider: str
    user: str


class RequestStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TokenRequest:
    id: str
    provider: str
    user: str
    status: RequestStatus = RequestStatus.PENDING
    token: Token | None = None
    error_code: str | None = None
    error: str | None = None

    def complete(self, token: Token) -> None:
        self.status = RequestStatus.DONE
        self.token = token

    def fail(self, code: str, message: str) -> None:
        self.status = RequestStatus.FAILED
        self.error_code = code
        self.error = message


class RequestOut(BaseModel):
    id: str
    status: RequestStatus
    provider: str
    user: str
    access_token: str | None = None
    token_type: str | None = None
    expire_time: str | None = None
    error_code: str | None = None
    error: str | None = None

    @classmethod
    def from_request(cls, req: TokenRequest) -> "RequestOut":
        out = cls(
            id=req.id,
            status=req.status,
            provider=req.provider,
            user=req.user,
            error_code=req.error_code,
            error=req.error,
        )
        if req.token:
            out.access_token = req.token.access_token
            out.token_type = req.token.token_type
            out.expire_time = req.token.expire_time
        return out
