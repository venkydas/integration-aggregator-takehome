import time
from collections.abc import Iterator

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BAO = "http://bao.test"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        public_url="http://agg.test",
        bao_addr=BAO,
        bao_token="test-token",
        workers=2,
    )


@pytest.fixture
def bao() -> Iterator[respx.MockRouter]:
    """Fake OpenBao: tests register the plugin responses they expect."""
    with respx.mock(base_url=f"{BAO}/v1/oauth2", assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(settings: Settings, bao: respx.MockRouter) -> Iterator[TestClient]:
    # The context manager runs the app lifespan, so the workers are live.
    with TestClient(create_app(settings)) as c:
        yield c


def poll(client: TestClient, location: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(location).json()
        if body["status"] != "pending":
            return body
        time.sleep(0.01)
    raise AssertionError(f"{location} still pending after {timeout}s")
