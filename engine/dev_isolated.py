"""Credential-free LabPro for integration tests (workbench proxy, browser, HTTP/WS).

`python -m engine.dev_isolated --port 8793 [--static desktop/dist]` starts the REAL web app (guard, proxy mode from
the TAL_WEB_* env, bridge, engine API) with every path to real credentials cut BEFORE anything is initialised:

* secrets: an in-memory keyring replaces the macOS Keychain (the `keyring` package is poisoned: touching it raises);
  the store starts empty, so no reference can resolve to a real value;
* providers: every LLM adapter construction raises (`isolated: provider calls are disabled`), on the free engine, the
  full graph and the Taiwan flows; OpenAI OAuth uses the in-memory store and its helper cannot reach a browser login;
* data dir: a fresh temp directory (never the user's), consent pre-accepted so the page is usable.

What still works: the stub debate (no model), watchlist, history, settings UI against the in-memory store, health,
the Taiwan symbol/source endpoints (public data, no credentials).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path


class MemKeyring:
    def __init__(self) -> None:
        self.d: dict[tuple[str, str], str] = {}

    def get_keyring(self):  # SecretStore.available() sees a non-fail backend
        return self

    def set_password(self, s, n, v):
        self.d[(s, n)] = v

    def get_password(self, s, n):
        return self.d.get((s, n))

    def delete_password(self, s, n):
        self.d.pop((s, n), None)


def _poison_keyring() -> None:
    """Any use of the real keyring after this point is a bug in the isolation: fail loudly."""
    import keyring

    def boom(*_a, **_k):
        raise RuntimeError("isolated: the real keyring must not be used")

    for name in ("get_password", "set_password", "delete_password", "get_keyring", "set_keyring"):
        setattr(keyring, name, boom)


class _DeadAdapter:
    """Looks like an adapter; every use fails (so endpoints report an error instead of crashing)."""

    async def open(self, *_a, **_k):
        raise RuntimeError("isolated: provider calls are disabled")

    complete = open

    async def close(self):
        return None


def _cut_providers() -> None:
    from . import live_debate, llm_providers

    def no_provider(*_a, **_k):
        raise RuntimeError("isolated: provider calls are disabled")

    dead = lambda *_a, **_k: _DeadAdapter()  # noqa: E731
    for mod in (llm_providers, live_debate):
        if hasattr(mod, "adapter_for"):
            mod.adapter_for = dead
    try:
        from . import server

        server.adapter_for = dead
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import full_debate

        full_debate.TradingAgentsGraph = no_provider        # the graph cannot be constructed => no model, no keys
    except Exception:  # noqa: BLE001
        pass


def build_isolated_app(*, port: int, static_dir: Path, data_dir: Path | None = None):
    os.environ.setdefault("TAL_SESSIONS_DB", str((data_dir or Path(tempfile.mkdtemp(prefix="tal-iso-"))) / "sessions.db"))
    _poison_keyring()
    from .secret_store import SecretStore
    from .web_app import ProxyConfig, build_web_app

    data_dir = data_dir or Path(os.environ["TAL_SESSIONS_DB"]).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    (data_dir / "consent.json").write_text('{"version": 1}')
    _cut_providers()
    app = build_web_app(
        token=secrets.token_urlsafe(24), port=port, data_dir=data_dir, static_dir=static_dir,
        launch_secret=secrets.token_urlsafe(24), cookie_value=secrets.token_urlsafe(32),
        store=SecretStore(MemKeyring()), proxy=ProxyConfig.from_env(),
    )
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m engine.dev_isolated")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--static", type=Path, default=Path(__file__).resolve().parents[1] / "desktop" / "dist")
    args = p.parse_args(argv)
    if args.port == 8765:
        sys.exit("refusing to use 8765 (the real server's port)")
    import uvicorn

    app = build_isolated_app(port=args.port, static_dir=args.static)
    print(f"isolated LabPro on 127.0.0.1:{args.port} (in-memory secrets, providers disabled)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
