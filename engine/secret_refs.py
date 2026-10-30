"""Resolve ``@@secret:<name>@@`` references on the server side.

In the web version the browser only ever holds opaque references. Anything that
carries credentials to a provider (the WebSocket start frame, /llm/test, ...)
passes through ``resolve`` right before use, so the real value exists only in
this process's memory for the duration of the call.
"""

from __future__ import annotations

from typing import Any, Optional

from .secret_store import REF_RE, SecretStore

_store: Optional[SecretStore] = None


def configure(store: Optional[SecretStore]) -> None:
    global _store
    _store = store


def resolve(obj: Any) -> Any:
    """Return a copy of `obj` with every exact-match reference replaced.

    A no-op when no store is configured (desktop build). An unknown reference is
    replaced by an empty string so the provider call fails loudly (401) rather
    than sending the literal reference text to a third party.
    """
    if _store is None:
        return obj
    if isinstance(obj, str):
        m = REF_RE.match(obj)
        if not m:
            return obj
        return _store.get(m.group(1)) or ""
    if isinstance(obj, dict):
        return {k: resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v) for v in obj]
    return obj
