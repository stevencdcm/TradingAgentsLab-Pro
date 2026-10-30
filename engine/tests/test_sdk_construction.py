"""Regression: every provider adapter must construct with the REAL installed SDK
(no network, fake key). An SDK major bump once changed the accepted timeout type
(anthropic 2.x uses httpx2) and broke every Anthropic call with a TypeError."""

from __future__ import annotations

import asyncio

import pytest

from engine.llm_providers import ProviderConfig, adapter_for

CASES = [
    ("openai", {"type": "api_key", "api_key": "k"}),
    ("anthropic", {"type": "api_key", "api_key": "k"}),
    ("openrouter", {"type": "api_key", "api_key": "k"}),
    ("gemini", {"type": "api_key", "api_key": "k"}),
    ("xai", {"type": "api_key", "api_key": "k"}),
    ("minimax", {"type": "api_key", "api_key": "k"}),
    ("local", {"type": "local", "base_url": "http://127.0.0.1:1/v1", "api_key": "k"}),
    ("local", {"type": "local", "base_url": "http://127.0.0.1:1/v1"}),
]


@pytest.mark.parametrize("provider,auth", CASES, ids=[f"{p}-{a['type']}{'-key' if a.get('api_key') else ''}" for p, a in CASES])
def test_adapter_opens_with_the_installed_sdk(provider, auth):
    cfg = ProviderConfig.from_dict({"provider": provider, "auth": auth, "model": "m"})
    adapter = adapter_for(cfg)

    async def go():
        await adapter.open(api_key=cfg.bearer_token)
        await adapter.close()

    asyncio.run(go())


def test_anthropic_client_gets_the_sdks_own_timeout_type():
    import anthropic

    cfg = ProviderConfig.from_dict({"provider": "anthropic", "auth": {"type": "api_key", "api_key": "k"}, "model": "m"})
    adapter = adapter_for(cfg)

    async def go():
        await adapter.open(api_key="k")
        t = adapter._client.timeout
        await adapter.close()
        return t

    t = asyncio.run(go())
    assert isinstance(t, anthropic.Timeout)
    assert (t.connect, t.read, t.write, t.pool) == (10.0, 90.0, 30.0, 10.0)
