from app.config import Settings


def test_empty_bao_token_means_unset(monkeypatch):
    monkeypatch.setenv("AGG_BAO_TOKEN", "")
    assert Settings().bao_token is None


def test_bao_token_from_env(monkeypatch):
    monkeypatch.setenv("AGG_BAO_TOKEN", "s.abc")
    assert Settings().bao_token.get_secret_value() == "s.abc"
