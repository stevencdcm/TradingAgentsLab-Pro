"""Dexter runner: the REAL Dexter agent loop (Agent.create/run) and the REAL read_file tool run in
the sandbox; only the MODEL is scripted (a fake Ollama server). Proves isolation, failure
handling and citation checking. It does NOT prove a real LLM follows the prompt (needs a real run).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from engine.dexter_job import run_dexter_job
from engine.tests.dexter_fake_ollama import FakeOllama, text, tool_call
from engine.tw_evidence import build_tw_evidence

DEXTER = Path(os.environ.get("DEXTER_ROOT", "/Users/steven/.local/share/her30/dexter"))
BUN = os.environ.get("TAL_BUN") or shutil.which("bun") or "/Users/steven/.local/share/her30/bun/bin/bun"
pytestmark = pytest.mark.skipif(not (DEXTER / "src/agent/agent.ts").exists() or not Path(BUN).exists(), reason="Dexter checkout or bun not installed")

FX = json.loads((Path(__file__).parent / "fixtures" / "tw" / "real_samples.json").read_text(encoding="utf-8"))
MODEL = {"provider": "ollama", "name": "ollama:fake", "route": "m3"}


@pytest.fixture(scope="module")
def pack_path(tmp_path_factory):
    import httpx

    def handler(req):
        url = str(req.url)
        if "finmindtrade" in url:
            tbl = {"TaiwanStockPrice": FX["price_2330"], "TaiwanStockMonthRevenue": FX["revenue_2330"],
                   "TaiwanStockFinancialStatements": FX["fin_2330"], "TaiwanStockInstitutionalInvestorsBuySell": FX["chips_2330"]}
            rows = [r for r in tbl[req.url.params["dataset"]] if r["date"] <= req.url.params["end_date"]]
            return httpx.Response(200, json={"msg": "success", "status": 200, "data": rows})
        return httpx.Response(200, json=FX["twse_row_2330"] if "twse" in url else FX["tpex_row_6488"])

    pack = build_tw_evidence("2330", "2026-10-29T16:30:00+08:00", client=httpx.Client(transport=httpx.MockTransport(handler)))
    p = tmp_path_factory.mktemp("ev") / "pack.json"
    p.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    return p


def run(pack_path, tmp_path, script, *, limits=None, delay=0.0, status=200, **kw):
    srv = FakeOllama(script, delay=delay, status=status)
    try:
        res = run_dexter_job(
            evidence_pack_path=pack_path, question="Summarise the latest price action.", model=MODEL, jobs_root=tmp_path / "jobs",
            dexter_root=DEXTER, provider_env={"OLLAMA_BASE_URL": srv.url}, limits=limits, bun=BUN, **kw,
        )
    finally:
        srv.close()
    return res, srv


GOOD = json.dumps({
    "findings": [{"id": "F1", "claim": "收盤價 2415 TWD", "numbers": [{"value": 2415, "field": "close"}],
                  "citations": [{"kind": "price", "period": "2026-10-29"}]}],
    "unknowns": ["還原股價缺"], "answer_markdown": "10/29 收盤 2415。",
}, ensure_ascii=False)


def test_success_with_a_verified_citation_and_measured_usage(pack_path, tmp_path):
    res, srv = run(pack_path, tmp_path, [tool_call("read_file", {"path": "evidence/pack.json"}), text(GOOD)])
    assert res["status"] == "succeeded" and res["reason"] is None, res
    f = res["findings"][0]
    assert f["verification"] == "verified" and f["citations"][0]["kind"] == "price" and f["point_in_time"] == "ok"
    assert [t["tool"] for t in res["tool_calls"]] == ["read_file"]
    assert res["usage"]["measured"] is True and res["usage"]["input_tokens"] >= 100 and res["usage"]["reasoning_tokens"] is None
    # Only read_file was offered to the model.
    offered = {t["function"]["name"] for t in srv.requests[0].get("tools", [])}
    assert offered == {"read_file"}


def test_injection_cannot_run_shell_write_files_or_browse(pack_path, tmp_path):
    marker = tmp_path / "pwned"
    script = [tool_call("bash", {"command": f"touch {marker}"}), tool_call("write_file", {"path": str(marker), "content": "x"}),
              tool_call("web_fetch", {"url": "http://example.com"}), text("I could not use those tools.")]
    res, srv = run(pack_path, tmp_path, script)
    assert not marker.exists()
    assert res["status"] != "succeeded"
    offered = {t["function"]["name"] for t in srv.requests[0].get("tools", [])}
    assert offered == {"read_file"}


def test_reads_outside_the_job_directory_are_refused(pack_path, tmp_path):
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET-CONTENT")
    script = [tool_call("read_file", {"path": str(secret)}), tool_call("read_file", {"path": "../../../../etc/hosts"}), text("done")]
    res, srv = run(pack_path, tmp_path, script)
    blob = json.dumps(srv.requests[-1], ensure_ascii=False)
    assert "TOP-SECRET-CONTENT" not in blob and "localhost" not in blob.lower().replace("ollama", "")
    assert res["status"] != "succeeded"


def test_max_iterations_is_a_failure_not_a_success(pack_path, tmp_path):
    res, _ = run(pack_path, tmp_path, lambda i, b: tool_call("read_file", {"path": "evidence/pack.json"}), limits={"max_iterations": 1})
    assert res["status"] == "failed" and res["reason"] == "max_iterations", res


def test_wall_clock_timeout(pack_path, tmp_path):
    res, _ = run(pack_path, tmp_path, [text(GOOD)], delay=5, limits={"wall_seconds": 1})
    assert res["status"] == "timeout", res


def test_provider_error_is_failed(pack_path, tmp_path):
    res, _ = run(pack_path, tmp_path, [text(GOOD)], status=500)
    assert res["status"] == "failed" and res["reason"] == "provider_error", res


@pytest.mark.parametrize(
    "answer,reason_part",
    [
        (json.dumps({"findings": [{"claim": "x", "numbers": [{"value": 2415}], "citations": [{"kind": "price", "period": "2026-10-31"}]}]}), "not found in the pack"),
        (json.dumps({"findings": [{"claim": "x", "numbers": [{"value": 9999, "field": "close"}], "citations": [{"kind": "price", "period": "2026-10-29"}]}]}), "not found in the cited items"),
        (json.dumps({"findings": [{"claim": "no citation at all"}]}), "no citation"),
    ],
)
def test_unverifiable_findings_never_become_success(pack_path, tmp_path, answer, reason_part):
    res, _ = run(pack_path, tmp_path, [tool_call("read_file", {"path": "evidence/pack.json"}), text(answer)])
    assert res["status"] == "partial" and res["reason"] == "no_verified_finding", res
    assert reason_part in res["findings"][0]["reason"]
    assert res["answer_markdown"] == ""            # an unverified summary is not surfaced


def test_a_finding_without_actually_reading_the_pack_is_unverified(pack_path, tmp_path):
    res, _ = run(pack_path, tmp_path, [text(GOOD)])
    assert res["status"] == "partial" and "never read" in res["findings"][0]["reason"]


def test_lookahead_citation_is_rejected(pack_path, tmp_path):
    # Re-stamp a pack whose price item "became available" after as_of.
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["as_of"] = "2026-10-28T09:00:00+08:00"
    from engine.tw_evidence import snapshot_hash

    pack["snapshot_id"] = snapshot_hash(pack["items"], pack["security_id"], pack["as_of"])
    p = tmp_path / "early.json"
    p.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    res, _ = run(p, tmp_path, [tool_call("read_file", {"path": "evidence/pack.json"}), text(GOOD)])
    assert res["status"] == "partial" and "look-ahead" in res["findings"][0]["reason"], res


def test_unparseable_answer_is_partial(pack_path, tmp_path):
    res, _ = run(pack_path, tmp_path, [tool_call("read_file", {"path": "evidence/pack.json"}), text("just prose, no JSON")])
    assert res["status"] == "partial" and res["reason"] == "unparseable_answer"


def test_tampered_or_missing_pack_is_refused_before_any_model_call(pack_path, tmp_path):
    bad = tmp_path / "bad.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["items"][0]["values"]["close"] = 1
    bad.write_text(json.dumps(pack), encoding="utf-8")
    res, srv = run(bad, tmp_path, [text(GOOD)])
    assert res["status"] == "failed" and res["reason"] == "bad_evidence_pack" and srv.requests == []
    res2 = run_dexter_job(evidence_pack_path=tmp_path / "nope.json", question="q", model=MODEL, jobs_root=tmp_path / "j", dexter_root=DEXTER, bun=BUN)
    assert res2["status"] == "failed" and res2["reason"] == "bad_evidence_pack"


def test_environment_is_whitelisted_and_home_is_the_job_dir(pack_path, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "LEAK-ME-NOT")
    monkeypatch.setenv("OPENAI_API_KEY", "LEAK-ME-NOT-2")
    res, srv = run(pack_path, tmp_path, [tool_call("read_file", {"path": "evidence/pack.json"}), text(GOOD)])
    jd = Path(res["job_dir"])
    assert jd.parent == (tmp_path / "jobs").resolve() and (jd / "evidence" / "pack.json").exists()
    assert not any("LEAK-ME-NOT" in json.dumps(r) for r in srv.requests)


def test_real_read_file_sandbox_rejects_escape_absolute_and_symlinks():
    import subprocess

    r = subprocess.run([BUN, "test", "sandbox.test.ts"], cwd=str(Path(__file__).resolve().parents[2] / "integrations" / "dexter-runner"),
                       env={**os.environ, "DEXTER_ROOT": str(DEXTER)}, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
