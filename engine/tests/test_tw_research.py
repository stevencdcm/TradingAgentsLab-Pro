"""Taiwan research flow end to end through the real WebSocket handler: evidence pack -> real Dexter
loop (read_file only) -> debate grounded on the pack. The MODEL is a scripted fake OpenAI-compatible
server that enforces a Bearer key; everything else is real. Not a real-model run."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.tests.dexter_fake_openai import KEY, FakeOpenAI

DEXTER = Path(os.environ.get("TAL_DEXTER_ROOT", "/Users/steven/.local/share/her30/dexter"))
BUN = shutil.which("bun") or "/Users/steven/.local/share/her30/bun/bin/bun"
pytestmark = pytest.mark.skipif(not (DEXTER / "src/agent/agent.ts").exists() or not Path(BUN).exists(), reason="Dexter/bun not installed")
FX = json.loads((Path(__file__).parent / "fixtures" / "tw" / "real_samples.json").read_text(encoding="utf-8"))
ANSWER = json.dumps({"findings": [{"id": "F1", "claim": "10/29 收盤 2415，較前一日下跌 10 元", "numbers": [{"value": 2415, "field": "close"}],
                                   "citations": [{"kind": "price", "period": "2026-10-29"}]}], "unknowns": ["還原股價缺"], "answer_markdown": "ok"}, ensure_ascii=False)


def _mock_client() -> httpx.Client:
    def handler(req):
        url = str(req.url)
        if "finmindtrade" in url:
            tbl = {"TaiwanStockPrice": FX["price_2330"], "TaiwanStockMonthRevenue": FX["revenue_2330"],
                   "TaiwanStockFinancialStatements": FX["fin_2330"], "TaiwanStockInstitutionalInvestorsBuySell": FX["chips_2330"]}
            rows = [r for r in tbl[req.url.params["dataset"]] if r["date"] <= req.url.params["end_date"]]
            return httpx.Response(200, json={"msg": "success", "status": 200, "data": rows})
        return httpx.Response(200, json=FX["twse_row_2330"] if "twse" in url else FX["tpex_row_6488"])
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def tw_env(tmp_db, tmp_path, monkeypatch):
    from engine import server as srv, tw_evidence, tw_research

    monkeypatch.setenv("TAL_EVIDENCE_DIR", str(tmp_path / "ev"))
    monkeypatch.setattr(tw_research, "DEXTER_ROOT", DEXTER)
    monkeypatch.setenv("TAL_BUN", BUN)
    real = tw_evidence.build_tw_evidence
    monkeypatch.setattr(tw_evidence, "build_tw_evidence", lambda code, as_of, **kw: real(code, as_of, client=_mock_client()))

    async def boom(**_):
        raise AssertionError("US data providers (yfinance) must never be used for a Taiwan code")

    monkeypatch.setattr(srv, "_fetch_summary_safe", boom)
    monkeypatch.setattr(srv, "_fetch_news_safe", boom)
    return srv


def _run(srv, base_url, *, ticker="2330", key=KEY, lang="zh-TW", provider_extra=None):
    token = "t"
    frame = {"ticker": ticker, "trade_date": "2026-10-29", "report_language": lang, "engine": "pro",
             "provider_config": {"provider": "local", "auth": {"type": "local", "base_url": base_url, **({"api_key": key} if key else {})},
                                 "model": "Q3.8 Flash Next 原版"}}
    events = []
    with TestClient(srv.build_app(token=token)) as c:
        with c.websocket_connect(f"/stream?token={token}") as ws:
            ws.send_json(frame)
            while True:
                ev = ws.receive_json()
                events.append(ev)
                if ev["type"] in ("session.complete", "run.error"):
                    break
    return events


def test_taiwan_flow_uses_evidence_runs_dexter_and_grounds_the_debate(tw_env):
    fake = FakeOpenAI(ANSWER)
    try:
        events = _run(tw_env, fake.base_url)
    finally:
        fake.close()
    types = [e["type"] for e in events]
    assert "run.error" not in types, [e for e in events if e["type"] == "run.error"]
    assert types[0] == "session.start" and events[0]["market"] == "TW" and events[0]["report_language"] == "zh-TW"
    ev_pack = next(e for e in events if e["type"] == "evidence.pack")
    assert ev_pack["security_id"] == "TW:2330" and ev_pack["unknowns"]
    summ = next(e for e in events if e["type"] == "data.summary")
    assert summ["source"].startswith("FinMind") and summ["as_of"] == "2026-10-29" and summ["last_close"] == 2415.0
    dex = next(e for e in events if e["type"] == "dexter.result")
    assert dex["status"] == "succeeded" and dex["findings"][0]["verification"] == "verified"
    assert [t["tool"] for t in dex["tool_calls"]] == ["read_file"] and dex["usage"]["measured"] is True
    msgs = [e for e in events if e["type"] == "agent.message"]
    assert len(msgs) == 12
    done = events[-1]
    assert done["type"] == "session.complete" and done["market"] == "TW"
    assert done["evidence"]["snapshot_id"] == ev_pack["snapshot_id"] and done["dexter"]["status"] == "succeeded"
    assert done["decision"]["stance"] == "moderately_bullish" and done["decision"]["rating_status"] == "parsed"
    # Every request (Dexter and debate) carried the Bearer key; the debate prompts hold the evidence + verified finding + zh-TW instruction.
    assert all(r["auth"] == f"Bearer {KEY}" for r in fake.requests)
    debate = [r["body"] for r in fake.requests if not r["body"].get("tools")]
    assert len(debate) == 12
    user0 = debate[0]["messages"][-1]["content"]
    assert "[price 2026-10-29]" in user0 and "(F1)" in user0 and "Dexter 已驗證發現" in user0
    assert "Traditional Chinese" in debate[0]["messages"][0]["content"]
    # Dexter was offered only read_file.
    dex_req = [r["body"] for r in fake.requests if r["body"].get("tools")]
    assert dex_req and {t["function"]["name"] for t in dex_req[0]["tools"]} == {"read_file"}


def test_failed_dexter_is_shown_and_the_debate_says_it_has_no_dexter_findings(tw_env):
    fake = FakeOpenAI("not json at all")
    try:
        events = _run(tw_env, fake.base_url)
    finally:
        fake.close()
    dex = next(e for e in events if e["type"] == "dexter.result")
    assert dex["status"] == "partial" and dex["reason"] == "unparseable_answer"
    debate = [r["body"] for r in fake.requests if not r["body"].get("tools")]
    assert "Dexter 未完成" in debate[0]["messages"][-1]["content"]
    assert events[-1]["dexter"]["status"] == "partial"


def test_oauth_provider_marks_dexter_unsupported_but_research_still_completes(tw_env):
    from engine.tw_research import dexter_model_and_env
    from engine.llm_providers import ProviderConfig

    cfg = ProviderConfig.from_dict({"provider": "openai", "auth": {"type": "oauth", "access": "a", "refresh": "r", "expires": 1}, "model": "m"})
    model, env, why = dexter_model_and_env(cfg)
    assert model is None and env == {} and "OAuth" in why


def test_stub_mode_taiwan_code_is_still_refused_with_an_explanation(tw_env):
    token = "t"
    with TestClient(tw_env.build_app(token=token)) as c:
        with c.websocket_connect(f"/stream?token={token}") as ws:
            ws.send_json({"ticker": "2330", "trade_date": "2026-10-29", "report_language": "zh-TW"})
            ev = ws.receive_json()
    assert ev["type"] == "run.error" and "台股" in ev["error"]
