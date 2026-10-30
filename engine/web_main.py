"""Entry point: `python -m engine.web [--port 8765] [--static desktop/dist] [--data-dir DIR]`.

Prints a one-time launch URL (also written, mode 0600, to <data-dir>/launch-url.txt).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "Trading Agents Lab Web"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m engine.web")
    p.add_argument("--port", type=int, default=int(os.environ.get("TAL_WEB_PORT", "8765")))
    p.add_argument("--static", type=Path, default=Path(__file__).resolve().parents[1] / "desktop" / "dist")
    p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("TAL_WEB_DATA_DIR", DEFAULT_DATA_DIR)))
    args = p.parse_args(argv)

    data_dir: Path = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    # The engine resolves its SQLite path at import time: set it BEFORE importing.
    os.environ.setdefault("TAL_SESSIONS_DB", str(data_dir / "sessions.db"))

    if not (args.static / "index.html").exists():
        sys.exit(f"renderer build not found at {args.static} (run `npm run build` in desktop/)")

    import uvicorn

    from .web_app import ProxyConfig, build_web_app

    token = secrets.token_urlsafe(24)
    launch_secret = secrets.token_urlsafe(24)
    # The session cookie value persists across restarts (0600 file in a 0700 dir) so a
    # restart does not sign the user out; the launch nonce and engine token are new each time.
    cookie_file = data_dir / "session.key"
    if cookie_file.exists() and cookie_file.stat().st_size >= 32:
        cookie_value = cookie_file.read_text().strip()
    else:
        cookie_value = secrets.token_urlsafe(32)
        cookie_file.write_text(cookie_value)
        os.chmod(cookie_file, 0o600)
    app = build_web_app(
        token=token,
        port=args.port,
        data_dir=data_dir,
        static_dir=args.static,
        launch_secret=launch_secret,
        cookie_value=cookie_value,
        proxy=ProxyConfig.from_env(),
    )
    # build_web_app wrote the single-use nonce to launch-url.txt (0600). SIGUSR1 issues a fresh
    # one (used by tools/open-web.sh after the first one was consumed) without printing it.
    import signal

    signal.signal(signal.SIGUSR1, lambda *_: app.state.launch.rotate())
    (data_dir / "web.pid").write_text(str(os.getpid()))
    os.chmod(data_dir / "web.pid", 0o600)
    print(f"Trading Agents Lab Pro (web) on http://127.0.0.1:{args.port}/  -  run tools/open-web.sh to open it", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
