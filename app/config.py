from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from AGG_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGG_")

    # Externally reachable base URL of this service; OAuth providers redirect
    # the user's browser to {public_url}/callback.
    public_url: str = "http://localhost:8080"

    bao_addr: str = "http://127.0.0.1:8200"
    bao_mount: str = "oauth2"
    bao_timeout_seconds: float = 10.0
    # Static token for local development only. In the cluster this stays unset
    # and the service logs in with its Kubernetes service account instead.
    bao_token: SecretStr | None = None
    bao_k8s_mount: str = "kubernetes"
    bao_k8s_role: str = "aggregator"
    k8s_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 (a path)

    workers: int = 8
    queue_size: int = 1000
    # How long a user has to finish the provider login after /connect.
    state_ttl_seconds: int = 600
    # How long a finished token request can be polled before it is dropped.
    request_ttl_seconds: int = 300

    @field_validator("bao_token")
    @classmethod
    def empty_token_means_unset(cls, v: SecretStr | None) -> SecretStr | None:
        # AGG_BAO_TOKEN="" should fall back to Kubernetes auth, not send "".
        return v if v and v.get_secret_value() else None
