import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import poll

GITHUB = {
    "name": "github",
    "provider": "github",
    "client_id": "cid",
    "client_secret": "super-secret",
}


# --- register provider ---------------------------------------------------------


def test_register_provider_sends_secret_to_bao_only(client, bao):
    route = bao.put("/servers/github").respond(204)

    r = client.post("/providers", json=GITHUB)

    assert r.status_code == 201
    assert r.json() == {"name": "github", "provider": "github"}
    assert "super-secret" not in r.text
    sent = json.loads(route.calls.last.request.content)
    assert sent["client_secret"] == "super-secret"
    assert sent["provider"] == "github"


@pytest.mark.parametrize("name", ["requests", "Bad Name", "a/b", "-x", ""])
def test_register_rejects_unsafe_or_reserved_names(client, name):
    r = client.post("/providers", json={**GITHUB, "name": name})
    assert r.status_code == 422


def test_register_rejects_unknown_provider_type(client):
    r = client.post("/providers", json={**GITHUB, "provider": "myspace"})
    assert r.status_code == 422


def test_validation_error_does_not_echo_secret(client):
    r = client.post("/providers", json={**GITHUB, "name": "BAD"})
    assert "super-secret" not in r.text


def test_bao_rejection_maps_to_400(client, bao):
    bao.put("/servers/github").respond(400, json={"errors": ["invalid provider options"]})
    r = client.post("/providers", json=GITHUB)
    assert r.status_code == 400
    assert r.json()["detail"] == ["invalid provider options"]


def test_bao_unreachable_maps_to_503(client, bao):
    bao.put("/servers/github").mock(side_effect=httpx.ConnectError("refused"))
    r = client.post("/providers", json=GITHUB)
    assert r.status_code == 503


# --- connect + callback --------------------------------------------------------


def _start_connect(client, bao, user="alice") -> str:
    """Run /connect and return the state the service generated."""
    bao.get("/servers/github").respond(200, json={"data": {"provider": "github"}})
    url_route = bao.put("/auth-code-url").respond(
        200, json={"data": {"url": "https://github.com/login/oauth/authorize?x=1"}}
    )
    r = client.post(f"/providers/github/users/{user}/connect")
    assert r.status_code == 200
    assert r.json()["authorization_url"].startswith("https://github.com/")
    sent = json.loads(url_route.calls.last.request.content)
    assert sent["redirect_url"] == "http://agg.test/callback"
    assert sent["scopes"] == ["read:user"]  # provider default
    return sent["state"]


def test_connect_then_callback_exchanges_code(client, bao):
    state = _start_connect(client, bao)
    creds = bao.put("/creds/github/alice").respond(204)

    r = client.get("/callback", params={"code": "abc", "state": state})

    assert r.status_code == 200
    assert r.json() == {"status": "connected", "provider": "github", "user": "alice"}
    sent = json.loads(creds.calls.last.request.content)
    assert sent == {"server": "github", "code": "abc", "redirect_url": "http://agg.test/callback"}


def test_state_is_single_use(client, bao):
    state = _start_connect(client, bao)
    bao.put("/creds/github/alice").respond(204)

    assert client.get("/callback", params={"code": "abc", "state": state}).status_code == 200
    assert client.get("/callback", params={"code": "abc", "state": state}).status_code == 400


def test_connect_accepts_custom_scopes(client, bao):
    bao.get("/servers/github").respond(200, json={"data": {"provider": "github"}})
    route = bao.put("/auth-code-url").respond(200, json={"data": {"url": "https://x"}})

    client.post("/providers/github/users/alice/connect", json={"scopes": ["repo"]})

    assert json.loads(route.calls.last.request.content)["scopes"] == ["repo"]


def test_connect_unknown_provider_is_404(client, bao):
    bao.get("/servers/nope").respond(404, json={"errors": []})
    assert client.post("/providers/nope/users/alice/connect").status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"code": "abc", "state": "never-issued"},
        {"code": "abc"},
        {"error": "access_denied", "state": "whatever"},
    ],
)
def test_callback_rejects_bad_requests(client, params):
    assert client.get("/callback", params=params).status_code == 400


def test_callback_with_invalid_code_is_400(client, bao):
    state = _start_connect(client, bao)
    bao.put("/creds/github/alice").respond(400, json={"errors": ["invalid grant"]})
    assert client.get("/callback", params={"code": "bad", "state": state}).status_code == 400


# --- async token retrieval -----------------------------------------------------


def test_token_request_is_async_and_returns_token(client, bao):
    bao.get("/creds/github/alice").respond(
        200,
        json={
            "data": {
                "access_token": "gho_123",
                "type": "Bearer",
                "expire_time": "2026-10-10T13:00:00Z",
            }
        },
    )

    r = client.get("/github/alice")

    assert r.status_code == 202
    location = r.headers["Location"]
    assert location.startswith("/requests/")
    body = poll(client, location)
    assert body["status"] == "done"
    assert body["access_token"] == "gho_123"
    assert body["token_type"] == "Bearer"
    assert body["expire_time"] == "2026-10-10T13:00:00Z"


def test_token_request_for_unconnected_user_fails(client, bao):
    bao.get("/creds/github/bob").respond(404, json={"errors": []})
    body = poll(client, client.get("/github/bob").headers["Location"])
    assert body["status"] == "failed"
    assert body["error_code"] == "not_connected"
    assert body["access_token"] is None


def test_token_request_when_refresh_fails(client, bao):
    bao.get("/creds/github/alice").respond(400, json={"errors": ["token expired"]})
    body = poll(client, client.get("/github/alice").headers["Location"])
    assert body["error_code"] == "token_unavailable"
    assert body["error"] == "token expired"


def test_token_request_when_bao_unreachable(client, bao):
    bao.get("/creds/github/alice").mock(side_effect=httpx.ConnectError("refused"))
    body = poll(client, client.get("/github/alice").headers["Location"])
    assert body["error_code"] == "openbao_unavailable"


def test_unexpected_worker_error_fails_request_instead_of_hanging(client, bao):
    bao.get("/creds/github/alice").respond(200, json={"unexpected": "shape"})
    body = poll(client, client.get("/github/alice").headers["Location"])
    assert body["status"] == "failed"
    assert body["error_code"] == "internal_error"


def test_unknown_request_is_404(client):
    assert client.get("/requests/does-not-exist").status_code == 404


@pytest.mark.parametrize("user", ["-alice", "al:ice", "alice."])
def test_token_request_rejects_unsafe_user_ids(client, user):
    assert client.get(f"/github/{user}").status_code == 422


def test_queue_full_returns_503(settings: Settings, bao):
    # No workers draining a queue of one: the second request cannot be accepted.
    tight = settings.model_copy(update={"workers": 0, "queue_size": 1})
    with TestClient(create_app(tight)) as c:
        assert c.get("/github/alice").status_code == 202
        r = c.get("/github/alice")
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "1"


# --- health --------------------------------------------------------------------


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_follows_bao_health(client, bao):
    health = bao.route(url="http://bao.test/v1/sys/health")
    health.respond(200)
    assert client.get("/readyz").status_code == 200
    health.respond(503)
    assert client.get("/readyz").status_code == 503
