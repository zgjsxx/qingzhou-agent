from __future__ import annotations

import os

from agent.llm_config import configure_provider_env, provider_model_kwargs


def test_anthropic_auth_token_is_mapped_as_token(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    configure_provider_env(
        adapter="anthropic",
        api_key="api-key",
        auth_token="auth-token",
        base_url="https://example.test",
    )

    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "auth-token"
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert os.environ["ANTHROPIC_API_URL"] == "https://example.test"


def test_anthropic_api_key_remains_supported(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    configure_provider_env(adapter="anthropic", api_key="api-key")

    assert os.environ["ANTHROPIC_API_KEY"] == "api-key"
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_anthropic_auth_token_adds_bearer_header() -> None:
    assert provider_model_kwargs(adapter="anthropic", auth_token="auth-token") == {
        "default_headers": {"Authorization": "Bearer auth-token"}
    }
