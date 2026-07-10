"""Shared LLM provider environment configuration."""

from __future__ import annotations

import os


def provider_model_kwargs(
    *,
    adapter: str,
    auth_token: str = "",
    base_url: str = "",
) -> dict[str, object]:
    """Return model options needed for provider-specific authentication."""
    kwargs: dict[str, object] = {}
    if adapter.strip().lower() == "anthropic":
        if auth_token:
            kwargs["default_headers"] = {"Authorization": f"Bearer {auth_token}"}
        if base_url:
            kwargs["base_url"] = base_url
    return kwargs


def configure_provider_env(
    *,
    adapter: str,
    api_key: str = "",
    auth_token: str = "",
    base_url: str = "",
) -> None:
    """Map project-level credentials to provider-specific environment variables."""
    provider = adapter.strip().lower()

    if provider == "anthropic":
        if auth_token:
            os.environ["ANTHROPIC_AUTH_TOKEN"] = auth_token
        elif api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if base_url:
            os.environ["ANTHROPIC_API_URL"] = base_url
            os.environ["ANTHROPIC_BASE_URL"] = base_url
    elif provider == "openai":
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            os.environ["OPENAI_BASE_URL"] = base_url
