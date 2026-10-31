"""Provider model discovery (engine/model_catalog.py + POST /llm/catalog): pagination, cache, failure keeps the last
good list, auth errors never echo a credential, only GET list requests (no inference) reach the provider."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from engine import model_catalog
from engine.server import build_app

KEY = "sk-ant-test-key-1234567890"
TOKEN = "t" * 32


def _model(i: str, **kw):
    d = {"type": "model", "id": i, "display_name": i.upper(), "created_at": "2026-01-01T00:00:00Z", "lifecycle": "active",
         "line": "sonnet", "max_input_tokens": 1_000_000, "max_tokens": 128_000, "deprecated_at": None, "retires_at": None,
         "capabilities": {"thinking": {"types": {"disabled": {"supported": False}}}}}
    d.update(kw)
    return d


class FakeCatalogServer:
    """Serves GET /v1/models with cursor pagination; `fail` switches to an error status. Records every request."""

    def __init__(self, models, page_size=2):
        self.models, self.page_size, self.fail, self.requests = models, page_size, None, []
        self.stuck_cursor = False
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                me.requests.append({"method": "GET", "path": u.path, "query": q, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if me.fail:
                    return self._send(me.fail, {"type": "error", "error": {"type": "x", "message": "echo " + (self.headers.get("x-api-key") or "")}})
                if self.headers.get("x-api-key") != KEY:
                    return self._send(401, {"type": "error", "error": {"message": "invalid x-api-key"}})
                ids = [m["id"] for m in me.models]
                start = ids.index(q["after_id"]) + 1 if q.get("after_id") in ids else 0
                chunk = me.models[start:start + me.page_size]
                has_more = start + me.page_size < len(me.models)
                last = chunk[-1]["id"] if chunk else None
                if me.stuck_cursor:
                    has_more, last = True, "same"
                self._send(200, {"data": chunk, "has_more": has_more, "first_id": chunk[0]["id"] if chunk else None, "last_id": last})

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def srv(monkeypatch):
    model_catalog._reset_cache_for_tests()
    s = FakeCatalogServer([_model(i) for i in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5")])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
    yield s
    s.close()
    model_catalog._reset_cache_for_tests()


def test_anthropic_paginates_and_lists_every_page(srv):
    out = model_catalog.list_models("anthropic", KEY)
    assert out["ok"] and out["source"] == "live"
    assert [m["id"] for m in out["models"]] == ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5"]
    assert len(srv.requests) == 3                                    # 5 models / page size 2
    assert srv.requests[0]["query"].get("after_id") is None and srv.requests[1]["query"]["after_id"] == "claude-opus-5-5"
    assert all(r["method"] == "GET" and r["path"] == "/v1/models" for r in srv.requests)   # discovery only, never inference
    assert srv.requests[0]["headers"]["x-api-key"] == KEY and "anthropic-version" in srv.requests[0]["headers"]


def test_retired_models_are_dropped_and_capabilities_kept(srv):
    srv.models.append(_model("claude-old-1", lifecycle="retired"))
    srv.models.append(_model("claude-dep-1", lifecycle="deprecated", retires_at="2027-01-01T00:00:00Z"))
    out = model_catalog.list_models("anthropic", KEY)
    ids = [m["id"] for m in out["models"]]
    assert "claude-old-1" not in ids and "claude-dep-1" in ids
    dep = next(m for m in out["models"] if m["id"] == "claude-dep-1")
    assert dep["lifecycle"] == "deprecated" and dep["retires_at"] == "2027-01-01T00:00:00Z"
    assert next(m for m in out["models"] if m["id"] == "claude-sonnet-5-5")["thinking_can_be_disabled"] is False


def test_cache_avoids_refetch_until_ttl_or_forced_refresh(srv):
    a = model_catalog.list_models("anthropic", KEY, now=1000.0)
    n = len(srv.requests)
    b = model_catalog.list_models("anthropic", KEY, now=1000.0 + 60)
    assert b["source"] == "cache" and b["fetched_at"] == a["fetched_at"] and len(srv.requests) == n
    c = model_catalog.list_models("anthropic", KEY, now=1000.0 + 61, force_refresh=True)
    assert c["source"] == "live" and len(srv.requests) == 2 * n
    d = model_catalog.list_models("anthropic", KEY, now=1000.0 + 61 + model_catalog.CACHE_TTL_S + 1)
    assert d["source"] == "live"


def test_failed_refresh_keeps_last_successful_catalog(srv):
    good = model_catalog.list_models("anthropic", KEY, now=1000.0)
    srv.fail = 500
    out = model_catalog.list_models("anthropic", KEY, now=2000.0, force_refresh=True)
    assert out["ok"] and out["source"] == "stale" and out["error"]["code"] == "provider_error"
    assert out["models"] == good["models"] and out["fetched_at"] == 1000.0


def test_first_failure_is_unavailable_and_credential_never_echoed(srv):
    srv.fail = 401
    out = model_catalog.list_models("anthropic", KEY)
    assert out["ok"] is False and out["source"] == "unavailable" and out["error"]["code"] == "auth"
    assert KEY not in json.dumps(out) and KEY[-8:] not in json.dumps(out)


def test_wrong_key_is_an_auth_error(srv):
    out = model_catalog.list_models("anthropic", "sk-ant-wrong-key-000000000")
    assert out["error"]["code"] == "auth" and out["models"] == []


def test_non_advancing_cursor_is_an_error_not_an_infinite_loop(srv):
    srv.stuck_cursor = True
    out = model_catalog.list_models("anthropic", KEY)
    assert out["ok"] is False and out["error"]["code"] == "bad_response"
    assert len(srv.requests) <= 3


def test_unreachable_server_is_a_network_error(monkeypatch):
    model_catalog._reset_cache_for_tests()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    out = model_catalog.list_models("anthropic", KEY)
    assert out["error"]["code"] in ("network", "timeout") and out["models"] == []


def test_missing_credential_and_unsupported_provider():
    assert model_catalog.list_models("anthropic", "")["error"]["code"] == "no_credential"
    out = model_catalog.list_models("minimax", "k")
    assert out["source"] == "unsupported" and out["ok"] is False


def test_cache_is_per_credential(srv):
    model_catalog.list_models("anthropic", KEY, now=1.0)
    n = len(srv.requests)
    out = model_catalog.list_models("anthropic", "sk-ant-other-key-0000000000", now=2.0)
    assert out["error"]["code"] == "auth" and len(srv.requests) == n + 1   # another account never reads KEY's cached list


# ---- pure parsers for the other providers ---------------------------------


def test_openai_filter_keeps_chat_models_only():
    body = {"data": [{"id": "gpt-5.5"}, {"id": "o4-mini"}, {"id": "text-embedding-3-large"}, {"id": "whisper-1"},
                     {"id": "gpt-image-1"}, {"id": "gpt-4o-realtime-preview"}, {"id": "omni-moderation-latest"}]}
    got = [m["id"] for m in model_catalog.parse_openai_style(body, model_catalog._OPENAI_CHAT_ID, model_catalog._OPENAI_EXCLUDE)]
    assert got == ["gpt-5.5", "o4-mini"]


def test_gemini_page_keeps_generate_content_models_and_token():
    body = {"models": [{"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro", "supportedGenerationMethods": ["generateContent"]},
                       {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "n"}
    models, tok = model_catalog.parse_gemini_page(body)
    assert [m["id"] for m in models] == ["gemini-2.5-pro"] and tok == "n"


def test_openrouter_skips_non_text_output_models():
    body = {"data": [{"id": "openai/gpt-5", "name": "GPT-5", "architecture": {"output_modalities": ["text"]}},
                     {"id": "x/imgen", "architecture": {"output_modalities": ["image"]}}]}
    assert [m["id"] for m in model_catalog.parse_openrouter(body)] == ["openai/gpt-5"]


def test_malformed_responses_raise_catalog_error():
    for fn, arg in ((model_catalog.parse_anthropic_page, []), (model_catalog.parse_openrouter, {}), (model_catalog.parse_gemini_page, "x")):
        with pytest.raises(model_catalog.CatalogError):
            fn(arg)


# ---- POST /llm/catalog -----------------------------------------------------


@pytest.fixture
def client(tmp_db):
    with TestClient(build_app(token=TOKEN)) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


CFG = {"provider": "anthropic", "auth": {"type": "api_key", "api_key": KEY}, "model": "claude-haiku-4-5"}


def test_endpoint_returns_live_catalog_without_the_key(client, srv):
    body = client.post("/llm/catalog", json={"provider_config": CFG}).json()
    assert body["ok"] and body["source"] == "live" and body["provider"] == "anthropic"
    assert "claude-sonnet-5-5" in [m["id"] for m in body["models"]]
    assert KEY not in json.dumps(body)
    again = client.post("/llm/catalog", json={"provider_config": CFG}).json()
    assert again["source"] == "cache"
    forced = client.post("/llm/catalog", json={"provider_config": CFG, "refresh": True}).json()
    assert forced["source"] == "live"


def test_endpoint_failed_refresh_keeps_list_and_reports_error(client, srv):
    client.post("/llm/catalog", json={"provider_config": CFG})
    srv.fail = 500
    body = client.post("/llm/catalog", json={"provider_config": CFG, "refresh": True}).json()
    assert body["ok"] and body["source"] == "stale" and body["models"] and body["error"]["code"] == "provider_error"


def test_endpoint_auth_error_has_no_secret(client, srv):
    cfg = {**CFG, "auth": {"type": "api_key", "api_key": "sk-ant-bad-key-1234567890"}}
    body = client.post("/llm/catalog", json={"provider_config": cfg}).json()
    assert body["ok"] is False and body["error"]["code"] == "auth"
    assert "sk-ant-bad-key-1234567890" not in json.dumps(body)


def test_endpoint_rejects_oauth_and_unsupported_provider(client):
    oauth = client.post("/llm/catalog", json={"provider_config": {"provider": "openai", "auth": {"type": "oauth", "access": "t"}}}).json()
    assert oauth["ok"] is False and oauth["source"] == "unsupported"
    mm = client.post("/llm/catalog", json={"provider_config": {"provider": "minimax", "auth": {"type": "api_key", "api_key": "k"}}}).json()
    assert mm["source"] == "unsupported"


def test_endpoint_requires_the_bearer_token(tmp_db):
    with TestClient(build_app(token=TOKEN)) as c:
        assert c.post("/llm/catalog", json={"provider_config": CFG}).status_code in (401, 403)


def test_endpoint_flags_models_without_an_exact_price(client, srv):
    srv.models.append(_model("claude-opus-9-9"))
    body = client.post("/llm/catalog", json={"provider_config": CFG, "refresh": True}).json()
    priced = {m["id"]: m["priced"] for m in body["models"]}
    assert priced["claude-opus-5-5"] is True and priced["claude-sonnet-5-5"] is True
    assert priced["claude-haiku-5-5"] is False              # tiered price: ceiling for CostGuard only, no exact price to show
    assert priced["claude-opus-9-9"] is False


def test_replaced_key_never_sees_the_previous_keys_list_even_when_its_own_fetch_fails(srv):
    model_catalog.list_models("anthropic", KEY, now=1.0)                        # account A cached
    srv.fail = 500
    out = model_catalog.list_models("anthropic", "sk-ant-account-b-0000000000", now=2.0, force_refresh=True)
    assert out["source"] == "unavailable" and out["models"] == []                # no stale copy of A's list for B
    srv.fail = None
    again = model_catalog.list_models("anthropic", KEY, now=3.0)
    assert again["source"] == "cache" and again["models"]                        # A's cache is intact


def test_rotating_back_to_the_old_key_after_the_new_one_failed_auth_is_isolated(srv):
    bad = "sk-ant-wrong-key-000000000"
    assert model_catalog.list_models("anthropic", bad)["error"]["code"] == "auth"
    ok = model_catalog.list_models("anthropic", KEY)
    assert ok["ok"] and ok["source"] == "live"


def test_a_model_retired_between_refreshes_disappears_from_the_next_list(srv):
    first = model_catalog.list_models("anthropic", KEY, now=1.0)
    assert "claude-haiku-4-5" in [m["id"] for m in first["models"]]
    srv.models[-1] = _model("claude-haiku-4-5", lifecycle="retired")
    second = model_catalog.list_models("anthropic", KEY, now=2.0, force_refresh=True)
    assert second["source"] == "live" and "claude-haiku-4-5" not in [m["id"] for m in second["models"]]
    srv.fail = 503                                                               # ...and a later failure keeps THAT list
    third = model_catalog.list_models("anthropic", KEY, now=3.0, force_refresh=True)
    assert third["source"] == "stale" and "claude-haiku-4-5" not in [m["id"] for m in third["models"]]


def test_only_retired_models_returned_is_an_error_that_keeps_the_last_list(srv):
    model_catalog.list_models("anthropic", KEY, now=1.0)
    srv.models = [_model("claude-old-1", lifecycle="retired")]
    out = model_catalog.list_models("anthropic", KEY, now=2.0, force_refresh=True)
    assert out["source"] == "stale" and out["error"]["code"] == "empty" and out["models"]
