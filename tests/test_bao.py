import json

import pytest
import respx

from app.bao import BaoClient, BaoError, NotFound
from app.config import Settings

BAO = "http://bao.test"


@pytest.fixture
def k8s_settings(tmp_path) -> Settings:
    jwt = tmp_path / "token"
    jwt.write_text("sa-jwt\n")
    return Settings(bao_addr=BAO, k8s_token_path=str(jwt))


def _login_route(mock: respx.MockRouter, *tokens: str) -> respx.Route:
    return mock.post(f"{BAO}/v1/auth/kubernetes/login").mock(
        side_effect=[
            respx.MockResponse(200, json={"auth": {"client_token": t, "lease_duration": 3600}})
            for t in tokens
        ]
    )


async def test_logs_in_with_service_account_and_reuses_token(k8s_settings):
    with respx.mock:
        login = _login_route(respx.mock, "bao-token-1")
        creds = respx.get(f"{BAO}/v1/oauth2/creds/github/alice").respond(
            200, json={"data": {"access_token": "gho_1"}}
        )
        client = BaoClient(k8s_settings)

        await client.get_token("github/alice")
        await client.get_token("github/alice")

        assert login.call_count == 1
        assert json.loads(login.calls.last.request.content) == {
            "role": "aggregator",
            "jwt": "sa-jwt",
        }
        assert creds.calls.last.request.headers["X-Vault-Token"] == "bao-token-1"
        await client.aclose()


async def test_relogs_in_once_when_token_rejected(k8s_settings):
    with respx.mock:
        login = _login_route(respx.mock, "stale", "fresh")
        respx.get(f"{BAO}/v1/oauth2/creds/github/alice").mock(
            side_effect=[
                respx.MockResponse(403, json={"errors": ["permission denied"]}),
                respx.MockResponse(200, json={"data": {"access_token": "gho_1"}}),
            ]
        )
        client = BaoClient(k8s_settings)

        token = await client.get_token("github/alice")

        assert token.access_token == "gho_1"
        assert login.call_count == 2
        await client.aclose()


async def test_error_mapping():
    client = BaoClient(Settings(bao_addr=BAO, bao_token="t"))
    with respx.mock:
        respx.get(f"{BAO}/v1/oauth2/creds/a/b").respond(404)
        respx.get(f"{BAO}/v1/oauth2/creds/a/c").respond(500, json={"errors": ["boom"]})
        with pytest.raises(NotFound):
            await client.get_token("a/b")
        with pytest.raises(BaoError) as exc:
            await client.get_token("a/c")
        assert exc.value.status == 500
        assert exc.value.errors == ["boom"]
    await client.aclose()
