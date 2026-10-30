"""FastAPI application factory + route definitions.

Phase 2 endpoints:
- GET  /health         — sanity ping
- POST /analyze        — one-shot analysis (stub)
- WS   /stream         — streaming agent debate (canned sequence)

Auth: all endpoints require `Authorization: Bearer <token>` header (or
`?token=...` query param on WebSocket since browsers can't set headers there).
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field

from . import __version__
from .data_providers import (
    BaseDataProvider,
    DataUnavailable,
    Headline,
    QuoteSummary,
    default_provider,
    provider_from_data_config,
)
from .live_debate import ProviderConfig, SentimentBlock, live_debate
from .report_language import normalize_report_language
from .llm_providers import adapter_for
from . import cost_guard, llm_providers, local_llm_detect, secret_refs, sentiment_sources, storage
from .run_manager import RunManager
from . import webhooks as webhook_dispatcher
from .stub_debate import canned_debate
from .telegram_bot import TelegramBot, TelegramBotConfig
from starlette.concurrency import run_in_threadpool
from .ticker import normalize_ticker, taiwan_code, taiwan_unsupported_message


def build_app(*, token: str) -> FastAPI:
    started_at = time.monotonic()

    # Phase 8c: the Telegram bot is owned by the app instance so the
    # lifespan hook can cleanly stop it on shutdown. Multiple in-process
    # apps (tests) get their own bot; renderer manages a single instance
    # in production via /telegram/start and /telegram/stop.
    telegram_bot = TelegramBot()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Engine starts with the bot stopped. Renderer must explicitly
        # call /telegram/start after the user enables it in Settings.
        # This keeps the engine bootable without any Telegram config and
        # lets the renderer carry the source-of-truth for enable state.
        try:
            yield
        finally:
            await telegram_bot.stop()

    app = FastAPI(
        title="TradingAgentsLab Engine",
        version=__version__,
        docs_url=None,        # Disable Swagger — sidecar is private
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.telegram_bot = telegram_bot

    # Renderer runs on http://localhost:5173 in dev (Vite) and on `file://`
    # (Origin: "null") in production-mode Electron — the built bundle loads
    # `dist/index.html` directly via Electron's file protocol. The sidecar
    # binds to 127.0.0.1 only and gates every request behind the bearer
    # token; CORS is not a security boundary here, it's just the browser's
    # preflight contract for non-simple requests. Use a wildcard so both
    # dev and prod work — and Playwright's e2e tests (also `file://`-loaded)
    # surfaced the prior production-mode breakage. WebSocket /stream is
    # unaffected by CORS but allow_methods covers OPTIONS preflight.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def require_bearer(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        # Trusted workbench proxy: authenticated by the web guard (hop secret + owner + Origin + session cookie).
        if request.scope.get("tal_session_authenticated") is True:
            return
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
            )
        if authorization.removeprefix("Bearer ") != token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid token",
            )

    bearer = [Depends(require_bearer)]

    @app.get("/health", dependencies=bearer)
    async def health() -> dict[str, Any]:
        from .llm_providers import _ALLOWED_PROVIDERS, _DEFAULT_MODELS

        return {
            "ok": True,
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - started_at, 2),
            # "engine_state" describes the *capability* of the engine. The
            # WS path uses the canned stub when no provider_config arrives in
            # the start frame, and the live path otherwise — so the engine
            # itself reports "ready" rather than "stub" or "live".
            "engine_state": "ready",
            "data_provider": default_provider.name,
            "live_supported": True,
            # Per-provider default models the engine assumes when ProviderConfig
            # arrives without an explicit `model` field. Renderer should also
            # ship its own defaults (PROVIDER_DEFAULT_MODEL); these are the
            # source of truth.
            "live_providers": sorted(_ALLOWED_PROVIDERS),
            "live_default_models": dict(_DEFAULT_MODELS),
            "storage_path": storage.db_path(),
        }

    @app.get("/data/summary", dependencies=bearer)
    async def data_summary(ticker: str, trade_date: str) -> dict[str, Any]:
        try:
            summary = await default_provider.quote_summary(
                ticker=ticker, trade_date=trade_date
            )
        except DataUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — convert any provider error into 502
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"data provider error: {exc}",
            )
        return _summary_to_dict(summary)

    @app.get("/data/news", dependencies=bearer)
    async def data_news(ticker: str, limit: int = 5) -> dict[str, Any]:
        try:
            headlines = await default_provider.news_headlines(
                ticker=ticker, limit=max(1, min(limit, 20))
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"news provider error: {exc}",
            )
        return {
            "ticker": ticker.upper(),
            "source": default_provider.name,
            "headlines": [_headline_to_dict(h) for h in headlines],
        }

    @app.get("/sessions", dependencies=bearer)
    async def list_sessions_endpoint(
        limit: int = 50, ticker: str | None = None
    ) -> dict[str, Any]:
        rows = storage.list_sessions(limit=limit, ticker=ticker)
        return {"sessions": [storage.summary_to_dict(r) for r in rows]}

    @app.get("/sessions/{session_id}", dependencies=bearer)
    async def get_session_endpoint(session_id: str) -> dict[str, Any]:
        detail = storage.get_session(session_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        return storage.detail_to_dict(detail)

    @app.delete("/sessions/{session_id}", dependencies=bearer)
    async def delete_session_endpoint(session_id: str) -> dict[str, Any]:
        deleted = storage.delete_session(session_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"session {session_id!r} not found",
            )
        return {"deleted": True, "id": session_id}

    @app.get("/watchlist", dependencies=bearer)
    async def list_watchlist_endpoint() -> dict[str, Any]:
        rows = storage.list_watchlist()
        return {"watchlist": [storage.watchlist_to_dict(r) for r in rows]}

    @app.post("/watchlist", dependencies=bearer)
    async def add_watchlist_endpoint(req: WatchlistAddRequest) -> dict[str, Any]:
        ticker, market, name = req.ticker, None, None
        tw = taiwan_code(req.ticker)
        if tw:
            # Canonical Taiwan identity = bare code + official market (never .TW/.TWO suffixes).
            from . import tw_symbols

            ticker = tw
            try:
                idx = await run_in_threadpool(tw_symbols.load_index)
                hit = tw_symbols.lookup(idx, tw)
            except tw_symbols.SymbolIndexError:
                hit = None
                if req.market:
                    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                        detail="Taiwan symbol index unavailable: cannot verify the market of this code")
            if hit is None and req.market:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{tw} is not in the official TWSE/TPEx index")
            if hit is not None:
                if req.market and req.market != hit["market"]:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{tw} is listed on {hit['market']}, not {req.market}")
                market, name = hit["market"], hit["name"]
        try:
            entry = storage.add_watchlist(ticker=ticker, note=req.note, market=market, name=name)
        except storage.WatchlistConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        return storage.watchlist_to_dict(entry)

    @app.get("/tw/symbols", dependencies=bearer)
    async def tw_symbols_search(q: str = "", limit: int = 20, refresh: bool = False) -> dict[str, Any]:
        """Search the official TWSE/TPEx symbol index by code or name ('2330' and '台積電' both work)."""
        from . import tw_symbols

        try:
            idx = await run_in_threadpool(tw_symbols.load_index, refresh=refresh)
        except tw_symbols.SymbolIndexError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True, "results": tw_symbols.search(idx, q, max(1, min(limit, 50))),
            "as_of": idx.get("as_of"), "fetched_at": idx.get("fetched_at"), "count": len(idx["entries"]),
            "stale": idx.get("stale", False), "refresh_error": idx.get("error"), "sources": idx.get("sources"),
        }

    @app.get("/tw/sources", dependencies=bearer)
    async def tw_sources_list() -> dict[str, Any]:
        """Taiwan data sources: shared catalog + what the adapters actually observed (never a static 'connected')."""
        from . import tw_graph_tools

        token_configured = False
        try:
            token_configured = secret_refs.has_named("data:finmind")
        except Exception:  # noqa: BLE001
            pass
        return {"sources": tw_graph_tools.sources_view(token_configured), "flows": ["pro_full", "simplified"]}

    @app.post("/tw/sources/test", dependencies=bearer)
    async def tw_sources_test(req: TwSourceTestRequest) -> dict[str, Any]:
        from . import tw_graph_tools

        token = None
        try:
            token = secret_refs.named_value("data:finmind")
        except Exception:  # noqa: BLE001
            pass
        res = await run_in_threadpool(tw_graph_tools.test_source, req.id, token)
        return {**res, "id": req.id}

    @app.delete("/watchlist/{ticker}", dependencies=bearer)
    async def remove_watchlist_endpoint(ticker: str) -> dict[str, Any]:
        removed = storage.remove_watchlist(ticker)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{ticker.upper()!r} not on the watchlist",
            )
        return {"removed": True, "ticker": ticker.upper()}

    @app.get("/cost-guard/state", dependencies=bearer)
    async def cost_guard_state() -> dict[str, Any]:
        spend, config = cost_guard.get_state()
        return {
            "spend": cost_guard.spend_to_dict(spend),
            "config": cost_guard.config_to_dict(config),
        }

    @app.put("/cost-guard/config", dependencies=bearer)
    async def cost_guard_update(req: CostGuardConfigRequest) -> dict[str, Any]:
        new_config = cost_guard.update_config(
            enabled=req.enabled,
            cap_daily_usd=req.cap_daily_usd,
            cap_weekly_usd=req.cap_weekly_usd,
            cap_monthly_usd=req.cap_monthly_usd,
            cap_sessions_per_day=req.cap_sessions_per_day,
        )
        return cost_guard.config_to_dict(new_config)

    @app.post("/cost-guard/check", dependencies=bearer)
    async def cost_guard_check(req: CostGuardCheckRequest) -> dict[str, Any]:
        result = cost_guard.check(
            model=req.model,
            auth_kind=req.auth_kind,
            max_tokens=req.max_tokens,
            extra_models=req.extra_models,
        )
        return cost_guard.check_result_to_dict(result)

    @app.post("/llm/test", dependencies=bearer)
    async def llm_test(req: LLMTestRequest) -> dict[str, Any]:
        """Validate stored LLM credentials with a 1-token completion.

        Explicit-trigger only (Settings → "Test connection" per row).
        Under ~$0.0001 per test for API-key providers; skips CostGuard
        entirely because this is a credential ping, not a debate.

        OAuth credentials live in the Electron main process and never
        reach the renderer, so OAuth is rejected here on principle;
        the OAuth row in Settings already shows live connection state
        via `oauth:openai:status`.
        """
        try:
            config = ProviderConfig.from_dict(secret_refs.resolve_provider_config(req.provider_config))
        except secret_refs.SecretRefError as exc:
            return {"ok": False, "error": str(exc)}
        if config is None:
            return {"ok": False, "error": "invalid provider_config"}
        if config.auth.get("type") == "oauth":
            return {
                "ok": False,
                "error": "OAuth credentials cannot be tested here; "
                "use the OAuth row's status indicator instead.",
            }
        adapter = adapter_for(config)
        started = time.monotonic()
        try:
            await adapter.open(api_key=config.bearer_token)
            await adapter.complete(
                system="",
                user="ping",
                model=config.model,
                max_tokens=1,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return {"ok": True, "model": config.model, "ms": elapsed_ms}
        except Exception as exc:  # noqa: BLE001 — surface whatever blew up
            return {"ok": False, "error": llm_providers.local_auth_hint(config, secret_refs.redact(f"{type(exc).__name__}: {exc}", config.credential))}
        finally:
            try:
                await adapter.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    @app.post("/llm/models", dependencies=bearer)
    async def llm_models(req: LLMModelsRequest) -> dict[str, Any]:
        """Models the user's ChatGPT account can use on the OAuth (Codex) path.

        Live from OpenAI's Codex catalog (see engine/codex_models.py), so the
        Analyze picker tracks OpenAI's retirements without an app release.
        Falls back to a small built-in list when the catalog is unreachable;
        `source` tells the renderer which one it got. OAuth credentials arrive
        per request (the renderer already sends them for live runs) and are
        never persisted by the engine.
        """
        from . import codex_models

        try:
            config = ProviderConfig.from_dict(secret_refs.resolve_provider_config(req.provider_config))
        except secret_refs.SecretRefError as exc:
            return {"ok": False, "error": str(exc)}
        if config is None or config.provider != "openai" or config.auth_kind != "oauth":
            return {"ok": False, "error": "model discovery is only for OpenAI OAuth"}
        try:
            models = await codex_models.list_codex_models(
                config.bearer_token,
                str(config.auth.get("account_id") or ""),
                force_refresh=req.refresh,
            )
        except codex_models.CodexModelsUnavailable as exc:
            return {
                "ok": True,
                "source": "fallback",
                "default": codex_models.FALLBACK_DEFAULT,
                "models": codex_models.FALLBACK_MODELS,
                "error": str(exc),
            }
        return {
            "ok": True,
            "source": "live",
            "default": models[0].slug,
            "models": [m.to_choice() for m in models],
        }

    @app.get("/telegram/status", dependencies=bearer)
    async def telegram_status() -> dict[str, Any]:
        return telegram_bot.status().to_dict()

    @app.post("/telegram/start", dependencies=bearer)
    async def telegram_start(req: TelegramBotStartRequest) -> dict[str, Any]:
        """Start (or restart) the bot polling loop.

        Token + allowlist + cap + provider config all come from the
        renderer's safeStorage. Engine never persists the bot token or
        provider credentials; per-chat daily spend IS persisted to a
        sibling JSON file so an app restart cannot reset the cap.
        """
        try:
            config = TelegramBotConfig.from_dict(req.model_dump())
            await telegram_bot.start(config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, don't leak stack
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        return telegram_bot.status().to_dict()

    @app.post("/telegram/stop", dependencies=bearer)
    async def telegram_stop() -> dict[str, Any]:
        await telegram_bot.stop()
        return telegram_bot.status().to_dict()

    @app.post("/telegram/approve", dependencies=bearer)
    async def telegram_approve(req: TelegramChatActionRequest) -> dict[str, Any]:
        """Move a pending chat_id into the live allowlist. The bot DMs the
        user "you're approved" on success. Idempotent: re-approving an
        already-approved chat returns ok without side effect."""
        ok = await telegram_bot.approve(req.chat_id)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"no pending request for chat_id {req.chat_id}",
            )
        return telegram_bot.status().to_dict()

    @app.post("/telegram/deny", dependencies=bearer)
    async def telegram_deny(req: TelegramChatActionRequest) -> dict[str, Any]:
        """Drop a pending entry without DMing the user. Returns 404 if
        there was no pending entry for the chat_id (already approved /
        denied / expired)."""
        ok = telegram_bot.deny(req.chat_id)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"no pending request for chat_id {req.chat_id}",
            )
        return telegram_bot.status().to_dict()

    @app.post("/telegram/refresh-credentials", dependencies=bearer)
    async def telegram_refresh_credentials(
        req: TelegramRefreshCredentialsRequest,
    ) -> dict[str, Any]:
        """Update the bot's provider_config in place. Used by the renderer
        to push fresh OpenAI OAuth access tokens on a periodic interval
        before the previous one expires; the bot otherwise wouldn't be
        able to run debates more than ~1hr after start when on OAuth.

        Returns 409 if the bot is not running. The renderer is expected
        to silently skip the refresh in that case rather than treat it
        as a hard error."""
        ok = telegram_bot.refresh_credentials(req.provider_config)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="telegram bot is not running; nothing to refresh",
            )
        return telegram_bot.status().to_dict()

    @app.post("/tw/evidence", dependencies=bearer)
    async def tw_evidence_build(req: TwEvidenceRequest) -> dict[str, Any]:
        """Build (and save) an immutable Taiwan evidence pack. No LLM, no cost.

        Free/anonymous FinMind + official TWSE/TPEx snapshots; gaps are listed under
        `unknowns`, never filled in. The saved file is what research jobs read.
        """
        import json as _json
        from pathlib import Path

        from starlette.concurrency import run_in_threadpool

        from . import tw_evidence

        code = taiwan_code(req.code)
        if not code:
            return {"ok": False, "error": "not a Taiwan stock code"}
        as_of = req.as_of or time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.gmtime(time.time() + 8 * 3600))
        try:
            fm_token = secret_refs.resolve_named(req.finmind_token, "data:finmind") if req.finmind_token else None
            pack = await run_in_threadpool(tw_evidence.build_tw_evidence, code, as_of, token=fm_token)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": secret_refs.redact(f"{type(exc).__name__}: {exc}")}
        out_dir = Path(os.environ.get("TAL_EVIDENCE_DIR") or storage._default_db_path().parent / "evidence")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{pack['security_id'].replace(':', '_')}-{pack['snapshot_id'][:16]}.json"
        path.write_text(_json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "snapshot_id": pack["snapshot_id"], "path": str(path), "coverage": pack["coverage"], "unknowns": pack["unknowns"]}

    @app.post("/llm/local-models", dependencies=bearer)
    async def llm_local_models(req: LocalModelsRequest) -> dict[str, Any]:
        """List the models an OpenAI-compatible local server exposes.

        Used by Settings → Local LLM for servers that require an API key (oMLX),
        which the unauthenticated auto-detect cannot see. The key arrives in the
        request body, is used for ONE GET, and is never logged or stored here.
        """
        base = req.base_url.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "error": "base_url must start with http:// or https://"}
        try:
            api_key = secret_refs.resolve_local_models_key(req.base_url, req.api_key) if req.api_key else None
        except secret_refs.SecretRefError as exc:
            return {"ok": False, "error": str(exc)}
        headers = {"Authorization": f"Bearer {api_key.strip()}"} if api_key and api_key.strip() else {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                res = await client.get(f"{base}/models", headers=headers)
        except Exception as exc:  # noqa: BLE001 — surface a short reason, never the key
            return {"ok": False, "error": f"{type(exc).__name__}"}
        if res.status_code != 200:
            reason = None
            if res.status_code in (401, 403):
                reason = "key_rejected" if api_key and api_key.strip() else "key_missing"
            return {"ok": False, "status": res.status_code, "error": f"HTTP {res.status_code}", **({"reason": reason} if reason else {})}
        try:
            data = res.json().get("data") or []
            models = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "unexpected response"}
        return {"ok": True, "models": models}

    @app.get("/llm/local-runtimes", dependencies=bearer)
    async def llm_local_runtimes() -> dict[str, Any]:
        """Probe localhost for OpenAI-compatible LLM runtimes.

        Empty `runtimes` array is a normal response, NOT an error — it
        means the user has no Ollama / LM Studio / llama.cpp server
        running. The renderer surfaces that as "Not detected" with a
        manual-entry fallback, not a failed fetch.
        """
        detected = await local_llm_detect.detect_runtimes()
        return {
            "runtimes": [local_llm_detect.runtime_to_dict(r) for r in detected],
        }

    @app.post("/cost-guard/reserve", dependencies=bearer)
    async def cost_guard_reserve(req: CostGuardReserveRequest) -> dict[str, Any]:
        try:
            result = cost_guard.reserve(
                model=req.model,
                auth_kind=req.auth_kind,
                max_tokens=req.max_tokens,
                override=req.override,
                extra_models=req.extra_models,
            )
        except cost_guard.CostGuardBlocked as exc:
            # 402 Payment Required — semantic match for "you can't afford
            # this without overriding"; pair with a JSON body the renderer
            # uses to populate the override modal.
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "cost_guard_blocked",
                    "over_dimension": exc.over_dimension,
                    "spend": cost_guard.spend_to_dict(exc.spend),
                    "config": cost_guard.config_to_dict(exc.config),
                    "est_cost_usd": round(exc.est_cost, 6),
                },
            )
        return cost_guard.reserve_result_to_dict(result)

    @app.post("/analyze", dependencies=bearer)
    async def analyze(req: AnalyzeRequest) -> dict[str, Any]:
        # Phase 2 stub — Phase 3+ wires this to the real tradingagents core.
        return {
            "ok": True,
            "ticker": req.ticker.upper(),
            "trade_date": req.trade_date,
            "decision": {
                "stance": "neutral",
                "conviction": 0.5,
                "bull_strength": 50,
                "bear_strength": 50,
                "risk_level": "moderate",
                "reasoning": (
                    "Stub response — engine not yet connected to tradingagents core."
                ),
            },
            "agents": [],
        }

    run_slot = {"busy": False}

    run_manager = RunManager()
    run_manager.recover()          # unfinished runs of a previous process -> interrupted (never re-run)
    app.state.run_manager = run_manager

    async def _execute_run(ws) -> None:
        """The analysis itself. `ws` is a RunSink: events go to the run (replayable, multi-subscriber), not to one socket."""

        captured_events: list[dict] = []
        slot_held = False
        tw_run_started = False
        tw_dexter_res = None
        # Bound before the try so the `except WebSocketDisconnect` handler can
        # log it even when the client disconnects before sending the start
        # frame (otherwise referencing `ticker` there raises NameError).
        ticker = ""
        try:
            # Wait for the client's start frame:
            #   {
            #     "ticker": "NVDA",
            #     "trade_date": "2026-05-08",
            #     "provider_config": {                  # optional
            #       "provider": "openai",
            #       "api_key": "sk-...",
            #       "model": "gpt-4o-mini",
            #       "max_tokens": 400
            #     }
            #   }
            # Web version: references to server-held secrets become real values here
            # (no-op in the desktop build). The browser never held the values.
            try:
                start = secret_refs.resolve_start_frame(await ws.receive_json())
                run_t0 = time.monotonic()      # whole-run wall clock starts when the start frame arrives
            except secret_refs.SecretRefError as exc:
                await ws.send_json({"type": "run.error", "error": str(exc), "code": "secret_ref_rejected"})
                await ws.close(code=1008, reason="secret_ref_rejected")
                return
            ticker = (start.get("ticker") or "").upper()
            trade_date = start.get("trade_date") or ""
            # Refuse a ticker we cannot route correctly BEFORE any provider call or
            # cost reservation: Taiwan codes (no Taiwan data source is connected) and
            # malformed symbols. Never run on a silently wrong or empty data source.
            guard_error: str | None = None
            tw = taiwan_code(ticker)
            tw_mode = False
            tw_flow_req = ""
            if tw:
                # Taiwan research needs a live model (the evidence-based flow); the stub
                # path has nothing to ground on, so it is refused with an explanation.
                if ProviderConfig.from_dict(start.get("provider_config")) is not None:
                    tw_mode = True
                    ticker = tw
                    # Taiwan flow choice. The Pro full graph is the default (the renderer always asks for Pro);
                    # the simplified evidence-pack + Dexter flow only runs when it is explicitly requested, so a
                    # Taiwan code with engine=pro never silently degrades to the simplified engine.
                    tw_flow_req = str(start.get("tw_flow") or "").lower()
                else:
                    guard_error = taiwan_unsupported_message(
                        tw, normalize_report_language(start.get("report_language"))
                    )
            else:
                try:
                    normalize_ticker(ticker)
                except ValueError as exc:
                    guard_error = f"Invalid ticker: {exc}"
            if guard_error:
                evt = {"type": "run.error", "error": guard_error, "code": "ticker_unsupported"}
                await ws.send_json(evt)
                await ws.close(code=1008, reason="ticker_unsupported")
                return
            config = ProviderConfig.from_dict(start.get("provider_config"))
            # Optional. Set by Analyze.tsx after a renderer-side cost-guard
            # check. If absent on a live debate, the WS handler auto-reserves
            # below for backward compatibility.
            reservation_id = start.get("reservation_id") or None
            # UI language -> report language. Reaches the prompts of BOTH engines
            # (and therefore every provider path); unknown values mean English.
            report_language = normalize_report_language(start.get("report_language"))
            # Pro engine selection. The free renderer never sends `engine`, so
            # the full-graph path is opt-in and the free path stays byte-for-byte
            # identical. `pro_config` carries the deep/quick model split, debate
            # round counts, effort, analyst selection, and an optional token cap.
            engine_mode = str(start.get("engine") or "").lower()
            is_pro = engine_mode in ("full", "pro")
            pro_config_raw = start.get("pro_config") or {}
            if not isinstance(pro_config_raw, dict):
                pro_config_raw = {}
            tw_full = tw_mode and (tw_flow_req == "full" or (tw_flow_req == "" and is_pro))
            tw_simple = tw_mode and not tw_full
            # The full graph mutates process-global library config / env while it runs, so only
            # one such run may be active; a second is refused explicitly rather than racing.
            if is_pro or tw_mode:
                try:
                    from . import full_debate as _fd_mod

                    _lingering = _fd_mod.graph_thread_still_running()      # a cancelled run's model call is still finishing
                except ImportError:                                         # no full-graph engine installed
                    _lingering = False
                if run_slot["busy"] or _lingering:
                    evt = {"type": "run.error", "code": "run_busy",
                           "error": "Another full-graph / Taiwan research run is already in progress. Wait for it to finish, then retry."}
                    # Same owner only: hand over the id of the run that holds the slot so the page can attach to it.
                    _run = getattr(ws, "run", None)
                    _holder = _run.mgr.get(_run.owner, run_slot.get("run_id") or "") if _run is not None else None
                    if _holder is not None and _holder.active:
                        evt["active_run_id"] = _holder.id
                    await ws.send_json(evt)
                    await ws.close(code=1008, reason="run_busy")
                    return
                run_slot["busy"] = True
                run_slot["run_id"] = getattr(getattr(ws, "run", None), "id", None)
                slot_held = True

            # Optional. Per-stream webhook configs (Phase 8a). Renderer sends
            # the full list each time; engine fires them after persist + before
            # ws.close(1000). URLs ARE secrets (Telegram/Discord embed bot
            # tokens), so never log them and never include in events.
            raw_hooks = start.get("webhooks") or []
            telegram_chat_ids = start.get("telegram_chat_ids") or {}
            webhooks: list[webhook_dispatcher.WebhookConfig] = []
            if isinstance(raw_hooks, list):
                for h in raw_hooks:
                    if isinstance(h, dict):
                        parsed = webhook_dispatcher.WebhookConfig.from_dict(h)
                        if parsed is not None:
                            webhooks.append(parsed)
            # Optional. When the renderer has Alpaca data keys configured,
            # it sends them as data_config = {provider: "alpaca", key_id, secret}.
            # Engine instantiates a per-stream AlpacaProvider; otherwise falls
            # through to the module-level yfinance default. No fallback chain
            # on Alpaca failure — if user configured Alpaca and it errors, the
            # data card stays empty so they notice (silent fall-through to
            # yfinance would mask configuration issues).
            data_provider: BaseDataProvider = (
                provider_from_data_config(start.get("data_config")) or default_provider
            )
            llm_label = (
                f"{config.provider}/{config.auth_kind}/{config.model}"
                if config is not None
                else "stub"
            )
            sys.stderr.write(
                f"[ws] OPEN ticker={ticker} date={trade_date} "
                f"data={data_provider.name} llm={llm_label}\n"
            )

            # Best-effort data fetch; debate degrades gracefully if it fails.
            if tw_full:
                from . import tw_graph_tools

                evt = await run_in_threadpool(tw_graph_tools.quote_summary_event, ticker, trade_date)
                if evt is not None:
                    await ws.send_json(evt)
                    captured_events.append(evt)
            summary = None if tw_mode else await _fetch_summary_safe(
                ticker=ticker, trade_date=trade_date, provider=data_provider
            )
            if summary is not None:
                evt = {"type": "data.summary", **_summary_to_dict(summary)}
                await ws.send_json(evt)
                captured_events.append(evt)

            headlines = [] if tw_mode else await _fetch_news_safe(
                ticker=ticker, limit=4, provider=data_provider
            )
            if headlines:
                evt = {
                    "type": "news.headlines",
                    "ticker": ticker,
                    "source": data_provider.name,
                    "headlines": [_headline_to_dict(h) for h in headlines],
                }
                await ws.send_json(evt)
                captured_events.append(evt)

            # Pre-fetch sentiment ONLY when we're about to run a live
            # debate. Stub mode skips entirely — the canned content
            # doesn't reference these sources and the fetch would just
            # burn ~6s of latency for nothing. Fetches run in parallel
            # via asyncio.gather; either failure surfaces as the
            # function's placeholder string, never as an exception.
            # The full graph's social analyst fetches its own sentiment via
            # tools, so this pre-fetch is skipped on the Pro path (wasted
            # latency + cost otherwise). The free live_debate still needs it.
            sentiment: Optional[SentimentBlock] = None
            if config is not None and not is_pro and not tw_mode:
                try:
                    spec = normalize_ticker(ticker)
                    twits_text, reddit_text = await asyncio.gather(
                        sentiment_sources.fetch_stocktwits_messages_async(
                            spec.base
                        ),
                        sentiment_sources.fetch_reddit_posts_async(
                            spec.base, asset_class=spec.asset_class
                        ),
                    )
                    sentiment = SentimentBlock(
                        stocktwits=twits_text, reddit=reddit_text
                    )
                    sys.stderr.write(
                        f"[ws] sentiment fetched ticker={ticker} "
                        f"twits_chars={len(twits_text)} reddit_chars={len(reddit_text)}\n"
                    )
                except Exception as exc:  # noqa: BLE001 — sentiment is supplemental
                    sys.stderr.write(
                        f"[ws] sentiment fetch error ticker={ticker}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    sentiment = None

            if config is not None:
                # Pro full-graph auth support: api_key (env-var injection) and
                # OpenAI OAuth (Codex factory patch — see engine/codex_llm.py).
                # `local` is supported: it maps onto the upstream library's generic
                # `openai_compatible` provider (base_url + optional key), see
                # `_build_pro_library_config`.
                if is_pro and not tw_simple and config.auth_kind not in ("api_key", "oauth", "local"):
                    evt = {
                        "type": "run.error",
                        "error": (
                            f"Pro engine requires an API key or OpenAI OAuth; "
                            f"{config.auth_kind} auth is not supported for the "
                            f"full-graph analysis."
                        ),
                    }
                    await ws.send_json(evt)
                    captured_events.append(evt)
                    await ws.close(code=1008, reason="pro_auth_unsupported")
                    return

                # The full graph runs on the deep/quick split; reserve against
                # the (pricier) deep model so the pre-flight budget gate is the
                # conservative one. free path reserves against its single model.
                # A malformed pro_config (e.g. non-numeric rounds) would raise
                # inside the translator; catch it and surface a run.error instead
                # of letting it close the socket as an opaque 1011. No reservation
                # exists yet at this point, so there is nothing to release.
                pro_lib_config: dict[str, Any] | None = None
                if is_pro:
                    try:
                        pro_lib_config = _build_pro_library_config(config, pro_config_raw)
                    except (ValueError, TypeError) as exc:
                        evt = {
                            "type": "run.error",
                            "error": f"Invalid Pro configuration: {exc}",
                        }
                        await ws.send_json(evt)
                        captured_events.append(evt)
                        await ws.close(code=1008, reason="invalid_pro_config")
                        return

                # OpenAI OAuth (Codex) self-heal: OpenAI retires ChatGPT-sign-in
                # models on its own schedule, so a saved/default model may no
                # longer exist for this account. Swap any unavailable model for
                # the account's current default from the live catalog before the
                # run starts, instead of failing every node with a 400. See
                # engine/codex_models.py. Cached per account, so this is one
                # catalog fetch per 15 minutes, not one per model.
                if config.provider == "openai" and config.auth_kind == "oauth" and not tw_simple:
                    from . import codex_models

                    oauth_acct = str(config.auth.get("account_id") or "")
                    oauth_token = config.bearer_token
                    config.model, _note = await codex_models.resolve_model(
                        config.model, oauth_token, oauth_acct
                    )
                    if pro_lib_config is not None:
                        for key in ("deep_think_llm", "quick_think_llm"):
                            pro_lib_config[key], _ = await codex_models.resolve_model(
                                pro_lib_config.get(key), oauth_token, oauth_acct
                            )

                # Select the Pro news analyst's get_news vendor from the user's
                # News-source preference (Part B). Upstream get_news ships only
                # yfinance/alpha_vantage; yfinance's Yahoo feed returns empty for
                # many equities, while Alpaca (which already feeds the chrome
                # headlines) is reliable. The chain always keeps yfinance as the
                # fallback so an empty primary never blanks the analyst.
                if is_pro and pro_lib_config is not None and not tw_full:
                    from . import alpaca_news

                    dc = start.get("data_config")
                    alpaca_ready = (
                        isinstance(dc, dict)
                        and str(dc.get("provider", "")).strip().lower() == "alpaca"
                        and bool(dc.get("key_id"))
                        and bool(dc.get("secret"))
                    )
                    chain = alpaca_news.news_vendor_chain(
                        str(pro_config_raw.get("news_source") or "auto"),
                        alpaca_ready,
                    )
                    if "alpaca" in chain:
                        alpaca_news.register_alpaca_news_vendor()
                    # Creds only when Alpaca is actually connected; cleared
                    # otherwise so a prior run's keys can never leak in, and so an
                    # "alpaca" preference without a connection degrades to Yahoo.
                    alpaca_news.set_alpaca_news_credentials(
                        dc.get("key_id") if alpaca_ready else None,
                        dc.get("secret") if alpaca_ready else None,
                    )
                    tv = dict(pro_lib_config.get("tool_vendors") or {})
                    tv["get_news"] = chain
                    pro_lib_config["tool_vendors"] = tv
                reserve_model = (
                    (pro_lib_config or {}).get("deep_think_llm") or config.model
                    if is_pro
                    else config.model
                )
                # EVERY model the run can bill must be priced: Pro deep AND quick, plus the main model (Dexter).
                guard_models = [m for m in ((pro_lib_config or {}).get("quick_think_llm") if is_pro else None, config.model) if m]
                if reservation_id and config.auth_kind not in ("oauth", "local") and cost_guard.get_config().enabled \
                        and cost_guard.unpriced_paid(reserve_model, config.auth_kind, guard_models) \
                        and not cost_guard.reservation_override(reservation_id):
                    evt = {"type": "cost.blocked", "over_dimension": "unknown_price",
                           "spend": cost_guard.spend_to_dict(cost_guard.check(model=reserve_model, auth_kind=config.auth_kind, max_tokens=config.max_tokens, extra_models=guard_models).current),
                           "config": cost_guard.config_to_dict(cost_guard.get_config()), "est_cost_usd": 0.0,
                           "message": "A model in this run has no verified price, so its cost is unknown. Use an explicit per-run override to continue."}
                    cost_guard.finalize_reservation(reservation_id, actual_cost_usd=0.0)       # nothing ran: release the held estimate
                    await ws.send_json(evt)
                    captured_events.append(evt)
                    await ws.close(code=1008, reason="cost_guard_blocked")
                    return

                # CostGuard auto-reserve when the renderer didn't pre-reserve.
                # Older renderer versions don't send reservation_id; rather
                # than reject them, we reserve server-side with override=False.
                # If the cap is exceeded, we send a structured error event
                # and close — renderer can surface this as a "configure caps
                # in Settings" hint.
                if not reservation_id:
                    try:
                        auto = cost_guard.reserve(
                            model=reserve_model,
                            auth_kind=config.auth_kind,
                            max_tokens=config.max_tokens,
                            override=False,
                            extra_models=guard_models,
                        )
                        reservation_id = auto.reservation_id
                    except cost_guard.CostGuardBlocked as exc:
                        evt = {
                            "type": "cost.blocked",
                            "over_dimension": exc.over_dimension,
                            "spend": cost_guard.spend_to_dict(exc.spend),
                            "config": cost_guard.config_to_dict(exc.config),
                            "est_cost_usd": round(exc.est_cost, 6),
                            "message": (
                                f"Live debate blocked: would exceed {exc.over_dimension} "
                                f"cap. Configure higher caps in Settings or use override."
                            ),
                        }
                        await ws.send_json(evt)
                        captured_events.append(evt)
                        await ws.close(code=1008, reason="cost_guard_blocked")
                        return

                # From here on something billable can start (Dexter, the graph, model calls): a run that ends abnormally is
                # accounted for (measured or estimated), not silently released. The run also knows which hold to keep alive.
                if getattr(ws, "run", None) is not None:
                    ws.run.reservation_id = reservation_id
                    ws.run.engine_started = True
                _cancel_check = (lambda: ws.run.cancel_requested) if getattr(ws, "run", None) is not None else None

                def _report_sink(rep: Optional[dict[str, Any]]) -> None:
                    """Latest partial official-price report -> the run record (persisted; shown if the run ends without a result)."""
                    run = getattr(ws, "run", None)
                    if run is None:
                        return
                    if tw_dexter_res is not None and not tw_simple:
                        from .tw_research import _report_with_dexter

                        rep = _report_with_dexter(rep, config, tw_dexter_res.get("ran", False), tw_dexter_res.get("raw_usage"))
                    run.set_cost_report(rep)

                # Both engines are async generators yielding the same event
                # shapes, so the send/capture loop is uniform. Pro routes to the
                # real LangGraph graph; free routes to the simplified reimpl.
                if tw_simple:
                    from .tw_research import tw_research

                    debate_events = tw_research(
                        code=ticker,
                        trade_date=trade_date,
                        config=config,
                        report_language=report_language,
                        reservation_id=reservation_id,
                        report_sink=_report_sink,
                    )
                elif is_pro:
                    # Imported lazily: the full graph pulls in langchain +
                    # the tradingagents library, which the free engine (and
                    # its test venv) does not carry. Keeping this off the
                    # module-load path means the free path boots without those
                    # deps and a Pro run surfaces a missing-dep as run.error.
                    try:
                        from .full_debate import full_debate
                    except Exception as exc:  # noqa: BLE001
                        evt = {
                            "type": "run.error",
                            "error": (
                                "Pro engine unavailable: the full-graph "
                                f"dependencies failed to load ({type(exc).__name__})."
                            ),
                        }
                        await ws.send_json(evt)
                        captured_events.append(evt)
                        # Release the reservation we just took so a boot-dep
                        # failure doesn't leave budget stuck as in-flight.
                        if reservation_id:
                            try:
                                cost_guard.finalize_reservation(
                                    reservation_id, actual_cost_usd=0.0
                                )
                            except Exception:  # noqa: BLE001
                                pass
                        await ws.close(code=1011, reason="pro_engine_unavailable")
                        return

                    raw_cap = pro_config_raw.get("token_cap")
                    token_cap = (
                        int(raw_cap)
                        if isinstance(raw_cap, (int, float)) and raw_cap > 0
                        else None
                    )
                    # Optional Alpha Vantage data key: unlocks the news / social
                    # / fundamentals analysts. Absent -> market-only (yfinance,
                    # free). Sent per-run (the engine persists no secrets).
                    av_key = start.get("alpha_vantage_key") or None
                    selected = pro_config_raw.get("selected_analysts") or None
                    if tw_full:
                        # Taiwan data tools for the full graph (FinMind free + TWSE/TPEx open data); no US fallback.
                        from . import tw_graph_tools

                        tw_graph_tools.install()
                        fm_token = None
                        try:
                            fm_token = secret_refs.resolve_named(start.get("finmind_token"), "data:finmind") if start.get("finmind_token") else None
                        except secret_refs.SecretRefError:
                            fm_token = None
                        tw_graph_tools.begin_run(trade_date, fm_token, str(start.get("tw_data_mode") or "").lower() or None)
                        tw_run_started = True
                        selected, dropped = tw_graph_tools.graph_analysts(selected)
                        lib_dv = {**(pro_lib_config.get("data_vendors") or {}), **tw_graph_tools.library_config()["data_vendors"]}
                        pro_lib_config = {**pro_lib_config, "data_vendors": lib_dv, "tool_vendors": {}}
                        notice = {
                            "type": "run.notice", "code": "taiwan_full_graph",
                            "data_mode": tw_graph_tools.mode(), "market": "TW",
                            "data_mode_label": ("歷史日期：只採用可證明當時已公開的資料" if tw_graph_tools.mode() == "historical" else "最新可得資料（截至選定日期）"),
                            "dropped_analysts": dropped,
                            "message": (
                                "台股 Pro 完整圖：價量／財報／新聞來自 FinMind 免費層與證交所／櫃買官方開放資料，不使用美股來源。"
                                + ("社群分析師已停用：沒有可靠的台股社群資料來源。" if dropped else "")
                                + ("歷史日期：只採用可證明當時已公開的資料，免費來源無法證明財報／新聞公布時間，故這些項目會缺漏。"
                                   if tw_graph_tools.mode() == "historical" else "最新資料模式：財報與新聞的公布時間未知，已標示。")
                            ),
                        }
                        await ws.send_json(notice)
                        captured_events.append(notice)
                        # Dexter pre-step over an evidence pack for the SAME trade date. It is shown in the stream and
                        # its usage/cost is folded into the run; a skip or failure is reported, never hidden.
                        from .tw_research import full_graph_dexter

                        tw_dexter_res = await full_graph_dexter(
                            code=ticker, trade_date=trade_date, config=config, lang=report_language,
                            enabled=start.get("tw_dexter") is not False,
                        )
                        tw_graph_tools.set_dexter(tw_dexter_res["block"])
                        _report_sink(None)                  # Dexter-only partial report until the graph reports its own
                        await ws.send_json(tw_dexter_res["event"])
                        captured_events.append(tw_dexter_res["event"])
                    debate_events = full_debate(
                        ticker,
                        trade_date,
                        config=pro_lib_config,
                        selected_analysts=selected,
                        token_cap=token_cap,
                        # REAL credential only (None for a keyless local runtime), so
                        # the "local" sentinel is never injected into the environment.
                        api_key=config.credential,
                        auth_kind=config.auth_kind,
                        # Codex backend 401s without the account id; present
                        # only on the OAuth auth shape (normalized upstream).
                        oauth_account_id=config.auth.get("account_id") or None,
                        reservation_id=reservation_id,
                        alpha_vantage_key=av_key,
                        report_language=report_language,
                        cancel_check=_cancel_check,
                        report_sink=_report_sink,
                    )
                else:
                    debate_events = live_debate(
                        ticker=ticker,
                        trade_date=trade_date,
                        summary=summary,
                        headlines=headlines,
                        config=config,
                        reservation_id=reservation_id,
                        sentiment=sentiment,
                        report_language=report_language,
                        report_sink=_report_sink,
                    )

                # Each agent.message arrives as the LLM response completes — we
                # throttle a little between events so the UI's transition
                # animations are still visible.
                hb_meta = {
                    "provider": config.provider, "model": config.model, "auth_kind": config.auth_kind,
                    "route": "taiwan" if tw_simple else ("full" if is_pro else "simplified"),
                }
                async with aclosing(with_heartbeat(debate_events, meta=hb_meta)) as beating:
                  async for event in beating:
                      if isinstance(event, dict) and event.get("type") == "run.heartbeat":
                          await ws.send_json(event)      # liveness only: not stored, no pacing sleep
                          continue
                      if isinstance(event, dict) and event.get("type") == "run.error" and isinstance(event.get("error"), str):
                          event = {**event, "error": llm_providers.local_auth_hint(config, event["error"])}
                      if isinstance(event, dict) and event.get("type") == "session.complete":
                          # Whole-run wall time (data fetch + Dexter + debate), frozen here and persisted once.
                          event = {**event, "elapsed_s": round(time.monotonic() - run_t0, 1)}
                          if tw_full:
                              from . import tw_graph_tools as _twg

                              from .tw_research import fold_dexter_into_complete

                              event = fold_dexter_into_complete(event, tw_dexter_res, config)
                              event = {**event, "market": "TW", "tw_flow": "full", "data_mode": _twg.mode(),
                                       "data_vendor": _twg.VENDOR, "source_health": _twg.source_health(),
                                       "limitations": _twg.limitations(report_language)}
                      await ws.send_json(event)
                      captured_events.append(event)
                      await asyncio.sleep(0.15)
            else:
                # Stub debate path — same UX, deterministic content.
                for event in canned_debate(
                    ticker=ticker,
                    trade_date=trade_date,
                    summary=summary,
                    headlines=headlines,
                ):
                    delay = event.pop("_delay", 0.4)
                    await ws.send_json(event)
                    captured_events.append(event)
                    await asyncio.sleep(delay)

            # Persist the completed session — best-effort, never fails the
            # stream. The user already saw the debate; persistence is bonus.
            session_id = _persist_session_safe(
                ticker=ticker,
                trade_date=trade_date,
                events=captured_events,
            )
            if getattr(ws, "run", None) is not None:
                ws.run.session_id = session_id          # exactly one history row per run; the run record points at it

            # Phase 8a — fire webhooks. After persist (so we have a session_id)
            # and before ws.close(1000) (so the renderer is still listening for
            # the `webhook.report` event). Best-effort: dispatch errors never
            # fail the stream. Total wall-clock cap is ~5s via per-receiver
            # timeout in dispatcher.
            if webhooks:
                complete_evt = next(
                    (e for e in captured_events if isinstance(e, dict) and e.get("type") == "session.complete"),
                    None,
                )
                if complete_evt:
                    raw_decision = complete_evt.get("decision") or {}
                    decision = raw_decision if isinstance(raw_decision, dict) else {}
                    try:
                        results = await webhook_dispatcher.dispatch_all(
                            configs=webhooks,
                            ticker=ticker,
                            trade_date=trade_date,
                            decision=decision,
                            session_id=session_id,
                            live=bool(complete_evt.get("live", False)),
                            provider=complete_evt.get("provider"),
                            model=complete_evt.get("model"),
                            estimated_cost_usd=complete_evt.get("estimated_cost_usd"),
                            telegram_chat_ids=(
                                {str(k): str(v) for k, v in telegram_chat_ids.items()}
                                if isinstance(telegram_chat_ids, dict)
                                else None
                            ),
                        )
                        report_evt = {
                            "type": "webhook.report",
                            "results": [
                                {**d, "error": secret_refs.scrub_urls_and_tokens(d["error"])} if isinstance(d.get("error"), str) else d
                                for d in (r.to_dict() for r in results)
                            ],
                        }
                        await ws.send_json(report_evt)
                        captured_events.append(report_evt)
                    except Exception as exc:  # noqa: BLE001 — webhooks must never break the stream
                        sys.stderr.write(
                            f"[webhooks] dispatch crashed: {type(exc).__name__}: {exc}\n"
                        )

            sys.stderr.write(
                f"[ws] CLOSE ticker={ticker} events={len(captured_events)} code=1000\n"
            )
            await ws.close(code=1000)
        except WebSocketDisconnect:
            sys.stderr.write(f"[ws] DISCONNECT ticker={ticker} mid-stream\n")
            return
        except Exception as exc:  # noqa: BLE001 — never end a run silently: say what failed (redacted) and release the hold
            import traceback

            msg = secret_refs.scrub_key_shapes(secret_refs.redact(f"{type(exc).__name__}: {exc}"))
            sys.stderr.write(f"[ws] ERROR ticker={ticker} {msg}\n" + secret_refs.scrub_key_shapes(secret_refs.redact(traceback.format_exc(limit=6))) + "\n")
            if reservation_id:
                # Local / OAuth cost nothing; a paid run that died mid-flight has an unknown cost (NULL), not 0.
                free = config is None or config.auth_kind in ("oauth", "local")
                cost_guard.finalize_reservation(reservation_id, actual_cost_usd=0.0 if free else None)
            try:
                await ws.send_json({"type": "run.error", "code": "engine_exception", "error": llm_providers.local_auth_hint(config, f"分析中斷（引擎內部錯誤）：{msg}")})
                await ws.close(code=1011, reason="engine_exception")
            except Exception:  # noqa: BLE001
                pass
            return
        finally:
            if tw_run_started:
                from . import tw_graph_tools

                tw_graph_tools.end_run()
            if slot_held:
                run_slot["busy"] = False
                run_slot["run_id"] = None

    def _owner(scope) -> str:
        """`local` for the loopback entry; the verified owner login behind the workbench proxy (set by the web guard)."""
        return str(scope.get("tal_owner") or "local")

    def _ws_authorized(ws: WebSocket) -> bool:
        return ws.query_params.get("token") == token or ws.scope.get("tal_session_authenticated") is True

    async def _first_frame(ws: WebSocket):
        try:
            return await ws.receive_json()
        except WebSocketDisconnect:
            sys.stderr.write("[ws] DISCONNECT ticker= before the start frame\n")
            return None

    @app.websocket("/stream")
    async def stream(ws: WebSocket) -> None:
        # Compatible entry: start frame in, the same events out. The run now lives in the RunManager, so closing this
        # socket (refresh, network drop) no longer cancels it; the first event is `run.attached` with the run id.
        if not _ws_authorized(ws):
            await ws.close(code=1008, reason="invalid token")
            return
        await ws.accept()
        raw = await _first_frame(ws)
        if not isinstance(raw, dict):
            return
        run, created = await run_manager.start_or_attach(_owner(ws.scope), raw, _execute_run)
        await run_manager.serve(ws, run, 0, resumed=not created, announce=bool(raw.get("resumable")))

    @app.websocket("/runs/{run_id}/stream")
    async def run_stream(ws: WebSocket, run_id: str) -> None:
        """Re-subscribe to a run: `?after=<last seq seen>` replays what was missed, then follows live."""
        if not _ws_authorized(ws):
            await ws.close(code=1008, reason="invalid token")
            return
        run = run_manager.get(_owner(ws.scope), run_id)
        if run is None:
            await ws.close(code=1008, reason="run_not_found")
            return
        try:
            after = max(0, int(ws.query_params.get("after", "0")))
        except ValueError:
            after = 0
        await ws.accept()
        await run_manager.serve(ws, run, after, resumed=True)

    @app.get("/runs/active", dependencies=bearer)
    async def runs_active(request: Request) -> dict[str, Any]:
        runs = run_manager.list_for(_owner(request.scope), 10)
        return {"runs": [r.status() for r in runs if r.active], "recent": [r.status() for r in runs if not r.active][:3]}

    @app.get("/runs/{run_id}", dependencies=bearer)
    async def run_status(run_id: str, request: Request) -> dict[str, Any]:
        run = run_manager.get(_owner(request.scope), run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run.status()

    @app.get("/runs/{run_id}/events", dependencies=bearer)
    async def run_events(run_id: str, request: Request, after: int = 0, limit: int = 500) -> dict[str, Any]:
        run = run_manager.get(_owner(request.scope), run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        run_manager._ensure_events(run)
        evs = [e for e in run.events if e["seq"] > max(0, after)][: max(1, min(limit, 2000))]
        return {"run_id": run.id, "state": run.state, "events": evs, "last_seq": run.seq, "replay_truncated": run.truncated}

    @app.post("/runs/{run_id}/cancel", dependencies=bearer)
    async def run_cancel(run_id: str, request: Request) -> dict[str, Any]:
        run = run_manager.get(_owner(request.scope), run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        await run_manager.cancel(run)
        return run.status()

    return app


HEARTBEAT_INTERVAL_S = float(os.environ.get("TAL_HEARTBEAT_S", "5"))


async def with_heartbeat(agen, *, meta: dict, interval: float = 0.0):
    """Yield every event of `agen` unchanged and, while it is silent, a lightweight
    `run.heartbeat` every `interval` seconds.

    A heartbeat proves only that the ENGINE PROCESS and the socket are alive and how long it has been
    since the last REAL event; it says nothing about whether the model is generating (the engines call
    models non-streaming, so there is nothing finer to report). It carries no content, no tokens and is
    never persisted. Cleanup of the wrapped generator is guaranteed on early exit / disconnect."""
    interval = interval or HEARTBEAT_INTERVAL_S
    it = agen.__aiter__()
    t0 = last = time.monotonic()
    pending = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            now = time.monotonic()
            if not done:
                yield {"type": "run.heartbeat", "elapsed_s": round(now - t0, 1), "since_event_s": round(now - last, 1), **meta}
                continue
            try:
                ev = pending.result()
            except StopAsyncIteration:
                return
            last = time.monotonic()
            pending = asyncio.ensure_future(it.__anext__())
            yield ev
    finally:
        pending.cancel()
        # Wait until the wrapped generator has REALLY finished (it may be draining an in-flight model call), even if this task is
        # cancelled again meanwhile: shield, and keep waiting; the generator's own deadline bounds it.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 — the generator raised: it is finished
                break
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except BaseException:  # noqa: BLE001
                pass


def _persist_session_safe(
    *, ticker: str, trade_date: str, events: list[dict]
) -> str | None:
    """Find session.complete in the captured events and write a row.

    Thin wrapper around `storage.write_session_from_events` so the WS
    handler keeps its existing call shape; the bot path uses the same
    helper directly. Returns the new session id, or None if the stream
    was aborted / the write failed. Caller uses the id as the
    `session_id` in webhook payloads so receivers can correlate alerts
    to History rows.
    """
    return storage.write_session_from_events(
        ticker=ticker, trade_date=trade_date, events=events
    )


async def _fetch_summary_safe(
    *,
    ticker: str,
    trade_date: str,
    provider: BaseDataProvider | None = None,
) -> QuoteSummary | None:
    if not ticker or not trade_date:
        return None
    p = provider or default_provider
    try:
        return await p.quote_summary(ticker=ticker, trade_date=trade_date)
    except Exception:  # noqa: BLE001 — never let data issues break the stream
        return None


async def _fetch_news_safe(
    *,
    ticker: str,
    limit: int,
    provider: BaseDataProvider | None = None,
) -> list[Headline]:
    if not ticker:
        return []
    p = provider or default_provider
    try:
        return await p.news_headlines(ticker=ticker, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _headline_to_dict(h: Headline) -> dict[str, Any]:
    return {
        "title": h.title,
        "publisher": h.publisher,
        "pub_date": h.pub_date,
        "url": h.url,
        "summary": h.summary,
    }


def _summary_to_dict(summary: QuoteSummary) -> dict[str, Any]:
    return {
        "ticker": summary.ticker,
        "trade_date": summary.trade_date,
        "as_of": summary.as_of,
        "last_close": summary.last_close,
        "period_open": summary.period_open,
        "period_high": summary.period_high,
        "period_low": summary.period_low,
        "period_change_pct": summary.period_change_pct,
        "avg_volume": summary.avg_volume,
        "sessions": summary.sessions,
        "source": summary.source,
        "asset_class": summary.asset_class,
    }


def _build_pro_library_config(config: ProviderConfig, pro_raw: dict) -> dict[str, Any]:
    """Translate the renderer's provider_config + pro_config into the upstream
    library config dict that `full_debate` merges over DEFAULT_CONFIG.

    The free path uses a single `model`; the full graph uses a deep/quick split,
    so deep/quick default to `config.model` when the renderer doesn't split them.
    Reasoning-effort is threaded under the provider-specific key the upstream
    `_get_provider_kwargs` reads. `full_debate._build_config` validates the
    required keys and fills every other default.
    """
    provider = config.provider
    # The app's "local" provider = any OpenAI-compatible server the user runs
    # (oMLX, vLLM, LM Studio, Ollama's /v1 ...). The upstream library models that
    # as the generic `openai_compatible` provider: base_url required, key optional,
    # structured output bound without a forced tool_choice (works across servers).
    if provider == "local":
        provider = "openai_compatible"
    deep = (pro_raw.get("deep_think_llm") or config.model or "").strip()
    quick = (pro_raw.get("quick_think_llm") or config.model or deep).strip()
    lib: dict[str, Any] = {
        "llm_provider": provider,
        "deep_think_llm": deep,
        "quick_think_llm": quick,
        "max_debate_rounds": int(pro_raw.get("max_debate_rounds", 1) or 1),
        "max_risk_discuss_rounds": int(pro_raw.get("max_risk_discuss_rounds", 1) or 1),
    }
    effort = str(pro_raw.get("effort") or "").strip()
    if effort:
        if provider == "anthropic":
            lib["anthropic_effort"] = effort
        elif provider == "openai":
            lib["openai_reasoning_effort"] = effort
        elif provider == "google":
            lib["google_thinking_level"] = effort
    # Local runtime endpoint (Ollama / LM Studio), when the auth shape carries it.
    base_url = config.auth.get("base_url") if isinstance(config.auth, dict) else None
    if base_url:
        lib["backend_url"] = base_url
    return lib


class AnalyzeRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=8)
    trade_date: str = Field(min_length=8, max_length=10, description="YYYY-MM-DD")


class WatchlistAddRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=8)
    note: str | None = Field(default=None, max_length=200)
    # Taiwan entries: official identity from the symbol index (validated server-side, not trusted).
    market: str | None = Field(default=None, max_length=8)
    name: str | None = Field(default=None, max_length=60)


class TwSourceTestRequest(BaseModel):
    id: str = Field(min_length=1, max_length=60)


class CostGuardConfigRequest(BaseModel):
    """All fields optional — only provided fields are updated (PATCH semantics
    on a PUT for simplicity)."""

    enabled: bool | None = None
    cap_daily_usd: float | None = Field(default=None, ge=0)
    cap_weekly_usd: float | None = Field(default=None, ge=0)
    cap_monthly_usd: float | None = Field(default=None, ge=0)
    cap_sessions_per_day: int | None = Field(default=None, ge=0)


class CostGuardCheckRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    # Other models the same run can bill (Pro quick model, ...): any unpriced one makes the run's cost unknown.
    extra_models: list[str] = Field(default_factory=list, max_length=6)
    # Allowed auth kinds: api_key, oauth, local. Local runs are $0 and
    # bypass the USD caps the same way OAuth does (see cost_guard.py
    # `_exceeds_any_cap`); validating it here keeps the pattern + the
    # downstream cost-guard logic in agreement.
    auth_kind: str = Field(pattern=r"^(api_key|oauth|local)$")
    max_tokens: int = Field(default=400, ge=1, le=8000)


class CostGuardReserveRequest(CostGuardCheckRequest):
    override: bool = False


class LLMModelsRequest(BaseModel):
    """OAuth provider_config for Codex model discovery. `refresh` bypasses
    the engine's 15-minute per-account catalog cache."""

    provider_config: dict[str, Any]
    refresh: bool = False


class TwEvidenceRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    as_of: str | None = Field(default=None, max_length=40)
    finmind_token: str | None = Field(default=None, max_length=500)


class LocalModelsRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)


class LLMTestRequest(BaseModel):
    """ProviderConfig dict the renderer would normally attach to a WS
    start frame. Re-validated through `ProviderConfig.from_dict` so the
    test endpoint enforces the same allowlist + auth-shape rules as a
    real debate."""

    provider_config: dict[str, Any]


class TelegramBotStartRequest(BaseModel):
    """Renderer payload to start (or restart) the Telegram bot.

    The token is a secret URL-embedded auth credential; it ships from the
    renderer's safeStorage on each /telegram/start call and is held in
    memory only by the engine. Restart-to-update is the supported way to
    rotate the token or update the allowlist."""

    token: str = Field(min_length=20, max_length=120)
    allowlist: list[int] = Field(default_factory=list)
    daily_cap_usd: float = Field(default=5.0, ge=0, le=1000)
    provider_config: dict[str, Any] = Field(default_factory=dict)


class TelegramChatActionRequest(BaseModel):
    """Approve/Deny target. Telegram chat_ids are int64 on the wire; the
    Pydantic int type handles that range natively."""

    chat_id: int


class TelegramRefreshCredentialsRequest(BaseModel):
    """Renderer pushes fresh provider_config (OAuth-aware) on a periodic
    interval so the engine always has a non-expired access token for the
    next bot-triggered debate."""

    provider_config: dict[str, Any]
