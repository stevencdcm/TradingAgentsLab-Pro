"""The isolated app cannot reach real credentials or providers. Runs in a SUBPROCESS: the isolation poisons the
`keyring` module and patches provider constructors process-wide, which must not leak into the other tests."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCRIPT = textwrap.dedent(
    """
    import json, sys, tempfile
    from pathlib import Path
    from fastapi.testclient import TestClient
    import keyring

    tmp = Path(tempfile.mkdtemp())
    static = tmp / "dist"; (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html><head></head><body>x</body></html>")
    from engine.dev_isolated import build_isolated_app
    app = build_isolated_app(port=8793, static_dir=static, data_dir=tmp / "data")
    out = {}

    # 1) the real keyring is unreachable (any use raises) -- so no Keychain item can ever be read
    try:
        keyring.get_password("TradingAgentsLab-Web", "llm:anthropic"); out["keyring"] = "REACHABLE"
    except RuntimeError as e:
        out["keyring"] = str(e)

    from engine.web_app import COOKIE_NAME
    c = TestClient(app, base_url="http://127.0.0.1:8793", client=("127.0.0.1", 5))
    # nonce flow works against the isolated app (read from its 0600 file, never printed)
    nonce = (tmp / "data" / "launch-url.txt").read_text().strip().split("?k=")[1]
    assert c.get(f"/?k={nonce}", follow_redirects=False).status_code == 302
    tok = c.get("/web/handshake").json()["token"]
    H = {"Authorization": f"Bearer {tok}"}

    # 2) stored secrets live in memory only and start empty: real references resolve to nothing
    out["secrets_empty"] = c.get("/web/secrets/list").json() == []
    out["ref_missing"] = c.post("/web/secrets/get", json={"key": "llm:anthropic"}).json() is None
    r = c.post("/llm/test", headers=H, json={"provider_config": {"provider": "anthropic", "auth": {"type": "api_key", "api_key": "@@secret:llm:anthropic@@"}, "model": "claude-haiku-4-5"}}).json()
    out["llm_test"] = r

    # 3) a provider can never be constructed, even with a literal key
    r2 = c.post("/llm/test", headers=H, json={"provider_config": {"provider": "openai", "auth": {"type": "api_key", "api_key": "sk-literal-not-real-0000000000"}, "model": "gpt-4o-mini"}}).json()
    out["llm_test_literal"] = r2

    # 4) the stub debate (no model) still runs end to end over the real WebSocket
    h = {"host": "127.0.0.1:8793", "cookie": f"{COOKIE_NAME}=" + c.cookies[COOKIE_NAME]}
    types = []
    with c.websocket_connect(f"/stream?token={tok}", headers=h) as ws:
        ws.send_json({"ticker": "AAPL", "trade_date": "2026-10-30"})
        for _ in range(4):
            types.append(ws.receive_json()["type"])
    out["stub_ws"] = types
    print("RESULT " + json.dumps(out))
    """
)


def test_isolated_app_cannot_reach_keychain_or_providers():
    p = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); exec(%r)" % (str(ROOT), SCRIPT)],
                       capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, (p.stdout[-800:], p.stderr[-1500:])
    import json

    out = json.loads(line[7:])
    assert "must not be used" in out["keyring"]
    assert out["secrets_empty"] is True and out["ref_missing"] is True
    assert out["llm_test"]["ok"] is False and "sk-" not in json.dumps(out["llm_test"])
    assert out["llm_test_literal"]["ok"] is False and "isolated: provider calls are disabled" in out["llm_test_literal"]["error"]
    assert out["stub_ws"] and out["stub_ws"][0] in ("data.summary", "session.start")
