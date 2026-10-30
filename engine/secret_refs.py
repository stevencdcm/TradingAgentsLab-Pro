"""Resolve ``@@secret:<name>@@`` references on the server side — purpose-bound.

In the web version the browser only holds opaque references. A reference is only ever resolved
for ONE documented purpose, and never anywhere else:

* provider credentials: `provider_config.auth.{api_key,access,refresh}` — and the secret NAME must
  belong to that provider (an Anthropic request cannot pull the OpenAI key, etc.);
* a local server's key may only be sent to the base URL the user SAVED for it (a request cannot
  redirect the key to an arbitrary host);
* data keys: `data_config.{key_id,secret}` and `alpha_vantage_key`, each bound to its own name;
* anything else carrying a reference (ticker, dates, webhook configs, pro_config ...) is REJECTED
  without echoing any value.

Resolved values stay in this process's memory only for the call; they are remembered solely so that
error text can be scrubbed (`redact`).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .secret_store import REF_RE, SecretStore

_store: Optional[SecretStore] = None
_seen: list[str] = []
_MAX_SEEN = 32


class SecretRefError(Exception):
    """A reference was used somewhere it is not allowed. The message never contains a value."""


def configure(store: Optional[SecretStore]) -> None:
    global _store
    _store = store


# provider -> {auth field -> allowed secret names}
_PROVIDER_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "openai": {"api_key": frozenset({"llm:openai"}), "access": frozenset({"oauth:openai:access"}), "refresh": frozenset({"oauth:openai:refresh"})},
    "anthropic": {"api_key": frozenset({"llm:anthropic"})},
    "openrouter": {"api_key": frozenset({"llm:openrouter"})},
    "gemini": {"api_key": frozenset({"llm:gemini"})},
    "xai": {"api_key": frozenset({"llm:xai"})},
    "minimax": {"api_key": frozenset({"llm:minimax"})},
    "local": {"api_key": frozenset({"local:api-key"})},
}
_DATA_FIELDS = {"key_id": "data:alpaca-key-id", "secret": "data:alpaca-secret"}
_AV_NAME = "data:alpha-vantage"
_FINMIND_NAME = "data:finmind"


def _remember(value: str) -> None:
    if len(value) >= 8 and value not in _seen:
        _seen.append(value)
        del _seen[:-_MAX_SEEN]


def _lookup(name: str) -> str:
    if _store is None:
        raise SecretRefError("secret references are not available")
    value = _store.get(name) or ""
    _remember(value)
    return value


def _norm_url(u: str) -> str:
    return str(u or "").strip().rstrip("/").lower()


def _ref_name(value: Any) -> Optional[str]:
    if isinstance(value, str):
        m = REF_RE.match(value)
        if m:
            return m.group(1)
    return None


def _has_ref(obj: Any) -> bool:
    if isinstance(obj, str):
        return bool(REF_RE.match(obj)) or "@@secret:" in obj
    if isinstance(obj, dict):
        return any(_has_ref(v) for v in obj.values()) or any(_has_ref(k) for k in obj)
    if isinstance(obj, list):
        return any(_has_ref(v) for v in obj)
    return False


def resolve_provider_config(cfg: Any) -> Any:
    """Resolve references in `provider_config.auth` for the provider's OWN secret names only."""
    if _store is None or not isinstance(cfg, dict):
        return cfg
    provider = str(cfg.get("provider") or "").lower()
    auth = cfg.get("auth")
    out = dict(cfg)
    if isinstance(auth, dict):
        new_auth = dict(auth)
        allowed = _PROVIDER_FIELDS.get(provider, {})
        for field, value in auth.items():
            name = _ref_name(value)
            if name is None:
                continue
            if field not in allowed or name not in allowed[field]:
                raise SecretRefError("secret reference not allowed for this provider/field")
            if provider == "local" and field == "api_key":
                saved = _store.get("local:base-url")
                if not saved or _norm_url(auth.get("base_url")) != _norm_url(saved):
                    raise SecretRefError("the local API key can only be sent to the saved local server")
            new_auth[field] = _lookup(name)
        out["auth"] = new_auth
    # No reference may remain anywhere else in the config.
    rest = {k: v for k, v in out.items() if k != "auth"}
    rest_auth = {k: v for k, v in (out.get("auth") or {}).items() if _ref_name(v) is None and _has_ref(v)}
    if _has_ref(rest) or rest_auth:
        raise SecretRefError("secret reference not allowed in this field")
    return out


def resolve_start_frame(start: Any) -> Any:
    """Resolve the references a run's start frame may legitimately carry; reject any other."""
    if _store is None or not isinstance(start, dict):
        return start
    out = dict(start)
    if "provider_config" in out:
        out["provider_config"] = resolve_provider_config(out["provider_config"])
    dc = out.get("data_config")
    if isinstance(dc, dict):
        new_dc = dict(dc)
        for field, allowed_name in _DATA_FIELDS.items():
            name = _ref_name(dc.get(field))
            if name is not None:
                if name != allowed_name:
                    raise SecretRefError("secret reference not allowed in this field")
                new_dc[field] = _lookup(name)
        out["data_config"] = new_dc
    name = _ref_name(out.get("alpha_vantage_key"))
    if name is not None:
        if name != _AV_NAME:
            raise SecretRefError("secret reference not allowed in this field")
        out["alpha_vantage_key"] = _lookup(name)
    name = _ref_name(out.get("finmind_token"))
    if name is not None:
        if name != _FINMIND_NAME:
            raise SecretRefError("secret reference not allowed in this field")
        out["finmind_token"] = _lookup(name)
    leftover = {k: v for k, v in out.items() if k not in ("provider_config",)}
    leftover_dc = {k: v for k, v in (out.get("data_config") or {}).items()} if isinstance(out.get("data_config"), dict) else {}
    # References that were resolved above are now plain values; whatever is still a reference is illegal.
    if _has_ref({k: v for k, v in leftover.items() if k != "data_config"}) or _has_ref(leftover_dc):
        raise SecretRefError("secret reference not allowed in this field")
    return out


def resolve_local_models_key(base_url: str, api_key: Optional[str]) -> Optional[str]:
    """Key for GET {base_url}/models: only 'local:api-key', only for the saved local base URL."""
    if api_key is None:
        return None
    name = _ref_name(api_key)
    if name is None:
        return api_key                      # a key the user just typed (not yet saved)
    if _store is None or name != "local:api-key":
        raise SecretRefError("secret reference not allowed in this field")
    saved = _store.get("local:base-url")
    if not saved or _norm_url(base_url) != _norm_url(saved):
        raise SecretRefError("the local API key can only be sent to the saved local server")
    return _lookup(name)


def resolve_named(value: Optional[str], allowed_name: str) -> Optional[str]:
    """A single field bound to exactly one secret name (e.g. the FinMind token)."""
    if value is None:
        return None
    name = _ref_name(value)
    if name is None:
        if "@@secret:" in value:
            raise SecretRefError("secret reference not allowed in this field")
        return value
    if name != allowed_name:
        raise SecretRefError("secret reference not allowed in this field")
    return _lookup(name)


def redact(text: str, *extra: Optional[str]) -> str:
    """Scrub resolved secrets (and the tail of any key echoed back) from outgoing text."""
    out = str(text)
    for value in list(_seen) + [v for v in extra if v and len(v) >= 8]:
        out = out.replace(value, "[redacted]")
        out = out.replace(value[-8:], "[redacted]")
    return out


_KEY_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{8,}|@@secret:[^@\s]+@@"
    r"|\b\d{6,}:[A-Za-z0-9_\-]{20,}|\bbot\d{6,}:[A-Za-z0-9_\-]{10,})"              # Telegram bot tokens (digits:token)
)
_URL = re.compile(r"https?://[^\s\"'<>]+")


def scrub_urls_and_tokens(text: str) -> str:
    """For error text that may echo a webhook endpoint (Telegram / Discord / Slack URLs ARE tokens): URLs become [url]."""
    return scrub_key_shapes(_URL.sub("[url]", str(text)))


def scrub_key_shapes(text: str) -> str:
    """Pattern-based scrub for text that may echo a provider key (API-key shape, Bearer header, secret reference),
    on top of `redact()` which only knows values this process has resolved."""
    return _KEY_SHAPES.sub("[redacted]", str(text))


def has_named(name: str) -> bool:
    """Whether a server-held secret exists (never returns its value)."""
    return _store is not None and _store.has(name)


def named_value(name: str) -> Optional[str]:
    """Server-side use only (e.g. the FinMind token for a connectivity test); never sent to the page."""
    return _lookup(name) if has_named(name) else None
