"""Refreshable provider model catalog (discovery only).

Why this module exists
----------------------
The app used to hardcode each provider's model list, so a new release (e.g. Claude 5.5) was missing from the pickers until
an app release. This module asks the provider's OFFICIAL model-listing endpoint which models exist, so the renderer can
merge that list with its built-in fallback.

What this is NOT (keep these honest in the UI too)
  * Discovery only. It never runs inference, so it bills no tokens. A model that appears here is "listed by the API for
    this key"; it is NOT proof that every request shape this app sends is accepted by that model.
  * Not pricing. Prices live in `llm_providers._COST_PER_M_TOKENS` / `official_pricing`; a discovered model with no exact
    price stays "unknown" (never $0). Nothing here changes what CostGuard reserves.
  * Not a selection. The caller keeps the user's saved choice untouched; this only widens what can be picked.

Credentials: resolved by the backend (`secret_refs.resolve_provider_config`) and sent only to the provider's own host.
They are never returned, logged, or included in error text.

Providers with an official list endpoint (no endpoint is invented): anthropic (GET /v1/models, cursor pagination),
openai (GET /v1/models), xai (GET /v1/models), openrouter (GET /api/v1/models), gemini (GET /v1beta/models, page token).
Others (minimax, local, Codex OAuth) are reported as unsupported here; local and Codex have their own discovery paths.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

CACHE_TTL_S = 15 * 60
_TIMEOUT_S = 10.0
_MAX_PAGES = 20          # hard stop on a misbehaving cursor
_PAGE_LIMIT = 1000       # Anthropic's documented maximum

ANTHROPIC_BASE_URL_DEFAULT = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class CatalogError(Exception):
    """A discovery failure. `code` is machine-readable; `message` never contains a credential."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Spec:
    base_url: Callable[[], str]
    fetch: Callable[[httpx.Client, str, str], list[dict]]   # (client, base_url, credential) -> normalised models


# ---- helpers ---------------------------------------------------------------


def _get_json(client: httpx.Client, url: str, *, headers: dict[str, str], params: Optional[dict] = None) -> Any:
    try:
        resp = client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as exc:
        raise CatalogError("timeout", "the provider did not answer in time") from exc
    except httpx.HTTPError as exc:
        raise CatalogError("network", f"could not reach the provider ({type(exc).__name__})") from exc
    if resp.status_code in (401, 403):
        raise CatalogError("auth", "the provider rejected the saved credential (check the key and its permissions)")
    if resp.status_code == 429:
        raise CatalogError("rate_limited", "the provider is rate limiting model listing; try again later")
    if resp.status_code >= 500:
        raise CatalogError("provider_error", f"the provider returned HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise CatalogError("http_error", f"unexpected HTTP {resp.status_code} from the provider")
    try:
        return resp.json()
    except ValueError as exc:
        raise CatalogError("bad_response", "the provider returned something that is not JSON") from exc


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


# ---- Anthropic -------------------------------------------------------------


def parse_anthropic_page(body: Any) -> tuple[list[dict], bool, Optional[str]]:
    """One GET /v1/models page -> (models, has_more, last_id). Pure, so the rules are unit-testable."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CatalogError("bad_response", "model list response has no 'data' array")
    out: list[dict] = []
    for item in body["data"]:
        if not isinstance(item, dict):
            continue
        mid = _s(item.get("id"))
        if not mid or item.get("type", "model") != "model":
            continue
        lifecycle = _s(item.get("lifecycle")) or "active"
        if lifecycle == "retired":          # inference naming it fails; never offer it
            continue
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        thinking = ((caps.get("thinking") or {}).get("types") or {}) if isinstance(caps.get("thinking"), dict) else {}
        disabled = thinking.get("disabled") if isinstance(thinking.get("disabled"), dict) else {}
        entry: dict[str, Any] = {
            "id": mid,
            "label": _s(item.get("display_name")) or mid,
            "lifecycle": lifecycle,
            "line": _s(item.get("line")) or None,
            "max_tokens": item.get("max_tokens") if isinstance(item.get("max_tokens"), int) else None,
            "max_input_tokens": item.get("max_input_tokens") if isinstance(item.get("max_input_tokens"), int) else None,
            "thinking_can_be_disabled": disabled.get("supported") if isinstance(disabled.get("supported"), bool) else None,
            "retires_at": _s(item.get("retires_at")) or None,
        }
        out.append(entry)
    return out, bool(body.get("has_more")), (_s(body.get("last_id")) or None)


def _fetch_anthropic(client: httpx.Client, base: str, key: str) -> list[dict]:
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "accept": "application/json"}
    models: list[dict] = []
    after: Optional[str] = None
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"limit": _PAGE_LIMIT}
        if after:
            params["after_id"] = after
        page, has_more, last_id = parse_anthropic_page(
            _get_json(client, f"{base.rstrip('/')}/v1/models", headers=headers, params=params))
        models.extend(page)
        if not has_more:
            break
        if not last_id or last_id in seen:     # a cursor that does not advance would loop forever
            raise CatalogError("bad_response", "model list pagination did not advance")
        seen.add(last_id)
        after = last_id
    else:
        raise CatalogError("bad_response", "model list has too many pages")
    return models


# ---- OpenAI-compatible (openai, xai) ---------------------------------------

# OpenAI lists embeddings, speech, image, moderation and fine-tune ids in the same endpoint. Keep only ids that look like
# chat/reasoning models; anything else is "not offered in the picker" rather than guessed to be usable.
_OPENAI_CHAT_ID = re.compile(r"^(gpt-|o\d|chatgpt-)", re.I)
_OPENAI_EXCLUDE = re.compile(r"(embedding|whisper|tts|transcribe|dall-e|image|moderation|realtime|audio|search|instruct|davinci|babbage|computer-use|codex)", re.I)
_XAI_CHAT_ID = re.compile(r"^grok-", re.I)
_XAI_EXCLUDE = re.compile(r"(image|imagine|video|vision-preview|embedding)", re.I)


def parse_openai_style(body: Any, include: re.Pattern, exclude: re.Pattern) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CatalogError("bad_response", "model list response has no 'data' array")
    out: list[dict] = []
    for item in body["data"]:
        mid = _s(item.get("id")) if isinstance(item, dict) else ""
        if mid and include.search(mid) and not exclude.search(mid):
            out.append({"id": mid, "label": mid})
    return out


def _fetch_openai(client: httpx.Client, base: str, key: str) -> list[dict]:
    body = _get_json(client, f"{base.rstrip('/')}/v1/models", headers={"Authorization": f"Bearer {key}"})
    return parse_openai_style(body, _OPENAI_CHAT_ID, _OPENAI_EXCLUDE)


def _fetch_xai(client: httpx.Client, base: str, key: str) -> list[dict]:
    body = _get_json(client, f"{base.rstrip('/')}/v1/models", headers={"Authorization": f"Bearer {key}"})
    return parse_openai_style(body, _XAI_CHAT_ID, _XAI_EXCLUDE)


# ---- OpenRouter ------------------------------------------------------------


def parse_openrouter(body: Any) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CatalogError("bad_response", "model list response has no 'data' array")
    out: list[dict] = []
    for item in body["data"]:
        if not isinstance(item, dict):
            continue
        mid = _s(item.get("id"))
        if not mid:
            continue
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        outputs = arch.get("output_modalities")
        if isinstance(outputs, list) and outputs and "text" not in outputs:   # image/audio-only generators
            continue
        out.append({"id": mid, "label": _s(item.get("name")) or mid})
    return out


def _fetch_openrouter(client: httpx.Client, base: str, key: str) -> list[dict]:
    body = _get_json(client, f"{base.rstrip('/')}/api/v1/models", headers={"Authorization": f"Bearer {key}"})
    return parse_openrouter(body)


# ---- Gemini ----------------------------------------------------------------


def parse_gemini_page(body: Any) -> tuple[list[dict], Optional[str]]:
    if not isinstance(body, dict) or not isinstance(body.get("models"), list):
        raise CatalogError("bad_response", "model list response has no 'models' array")
    out: list[dict] = []
    for item in body["models"]:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods")
        if not isinstance(methods, list) or "generateContent" not in methods:   # embeddings etc.
            continue
        mid = _s(item.get("name")).removeprefix("models/")
        if mid:
            out.append({"id": mid, "label": _s(item.get("displayName")) or mid})
    return out, (_s(body.get("nextPageToken")) or None)


def _fetch_gemini(client: httpx.Client, base: str, key: str) -> list[dict]:
    models: list[dict] = []
    token: Optional[str] = None
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        page, token = parse_gemini_page(_get_json(client, f"{base.rstrip('/')}/v1beta/models",
                                                  headers={"x-goog-api-key": key}, params=params))
        models.extend(page)
        if not token:
            return models
        if token in seen:
            raise CatalogError("bad_response", "model list pagination did not advance")
        seen.add(token)
    raise CatalogError("bad_response", "model list has too many pages")


SPECS: dict[str, _Spec] = {
    "anthropic": _Spec(lambda: os.environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_BASE_URL_DEFAULT, _fetch_anthropic),
    "openai": _Spec(lambda: "https://api.openai.com", _fetch_openai),
    "xai": _Spec(lambda: "https://api.x.ai", _fetch_xai),
    "openrouter": _Spec(lambda: "https://openrouter.ai", _fetch_openrouter),
    "gemini": _Spec(lambda: "https://generativelanguage.googleapis.com", _fetch_gemini),
}


def supported_providers() -> list[str]:
    return sorted(SPECS)


# ---- cache + public entry point --------------------------------------------

# (provider, credential fingerprint) -> (fetched_at_epoch, models). The fingerprint is a one-way hash so the cache never
# holds the key, and two different accounts never see each other's list.
_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def _fingerprint(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]


def _result(provider: str, source: str, fetched_at: Optional[float], models: list[dict], error: Optional[CatalogError] = None) -> dict:
    out: dict[str, Any] = {"ok": error is None or bool(models), "provider": provider, "source": source,
                           "fetched_at": fetched_at, "ttl_s": CACHE_TTL_S, "models": models}
    if error is not None:
        out["error"] = {"code": error.code, "message": error.message}
    return out


def list_models(provider: str, credential: str, *, force_refresh: bool = False, now: Optional[float] = None) -> dict:
    """Catalog for one provider/credential. Always returns a dict (never raises).

    source: 'live' (just fetched), 'cache' (within the TTL, no network), 'stale' (refresh failed, last successful list
    kept), 'unavailable' (refresh failed and there is nothing earlier), 'unsupported' (no official list endpoint).
    A failed refresh NEVER discards the last successful catalog.
    """
    provider = (provider or "").lower()
    spec = SPECS.get(provider)
    if spec is None:
        return {"ok": False, "provider": provider, "source": "unsupported", "fetched_at": None, "ttl_s": CACHE_TTL_S, "models": [],
                "error": {"code": "unsupported", "message": "this provider has no official model-listing endpoint the app uses"}}
    if not credential:
        return _result(provider, "unavailable", None, [], CatalogError("no_credential", "no saved credential for this provider"))
    clock = time.time() if now is None else now
    key = (provider, _fingerprint(credential))
    cached = _cache.get(key)
    if cached and not force_refresh and clock - cached[0] < CACHE_TTL_S:
        return _result(provider, "cache", cached[0], cached[1])
    try:
        with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False) as client:
            models = spec.fetch(client, spec.base_url(), credential)
    except CatalogError as exc:
        if cached:
            return _result(provider, "stale", cached[0], cached[1], exc)
        return _result(provider, "unavailable", None, [], exc)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not surface a stack trace or a credential
        err = CatalogError("internal", f"unexpected error while listing models ({type(exc).__name__})")
        if cached:
            return _result(provider, "stale", cached[0], cached[1], err)
        return _result(provider, "unavailable", None, [], err)
    if not models:   # an empty list is more likely a filter/format problem than "the account has no models": keep the old one
        err = CatalogError("empty", "the provider returned no selectable models")
        if cached:
            return _result(provider, "stale", cached[0], cached[1], err)
        return _result(provider, "unavailable", None, [], err)
    _cache[key] = (clock, models)
    return _result(provider, "live", clock, models)


def _reset_cache_for_tests() -> None:
    _cache.clear()
