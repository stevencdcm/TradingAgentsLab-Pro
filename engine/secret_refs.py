"""Resolve ``@@secret:<name>@@`` references on the server side.

In the web version the browser only ever holds opaque references. Anything that
carries credentials to a provider (the WebSocket start frame, /llm/test, ...)
passes through ``resolve`` right before use, so the real value exists only in
this process's memory for the duration of the call.
"""

from __future__ import annotations

from typing import Any, Optional

import re

from .secret_store import REF_RE, SecretStore

_store: Optional[SecretStore] = None

# Only credentials meant for provider/data calls may be resolved. Anything else
# (webhook configs, bot tokens, arbitrary names) resolves to "".
RESOLVABLE = re.compile(
    r"^(llm:[a-z0-9_\-]+|local:api-key|data:[a-z0-9_\-]+|oauth:openai:(access|refresh))$"
)

# Values resolved in this process, kept ONLY so error text can be scrubbed.
_seen: list[str] = []
_MAX_SEEN = 32


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
        name = m.group(1)
        if not RESOLVABLE.match(name):
            return ""
        value = _store.get(name) or ""
        if len(value) >= 8 and value not in _seen:
            _seen.append(value)
            del _seen[:-_MAX_SEEN]
        return value
    if isinstance(obj, dict):
        return {k: resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve(v) for v in obj]
    return obj


def redact(text: str, *extra: Optional[str]) -> str:
    """Scrub resolved secrets (and the tail of any key echoed back) from outgoing text.

    Provider error messages sometimes echo a key ("Incorrect API key provided:
    sk-...abcd"). Applied to every error string that can reach the page.
    """
    out = str(text)
    for value in list(_seen) + [v for v in extra if v and len(v) >= 8]:
        out = out.replace(value, "[redacted]")
        out = out.replace(value[-8:], "[redacted]")
    return out
