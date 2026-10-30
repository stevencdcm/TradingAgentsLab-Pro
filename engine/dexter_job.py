"""Run one Dexter research job as a sandboxed child process (S5).

The Python side owns everything the runner must not decide for itself:
tamper-checking the evidence pack, the environment whitelist, the per-job
directory, wall-clock kill of the whole process group, and refusing to turn a
crash / bad output into a "success". It never writes a Dexter conclusion of its
own: no result from the child means `failed`, full stop.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .tw_evidence import snapshot_hash

RUNNER_DIR = Path(__file__).resolve().parents[1] / "integrations" / "dexter-runner"
REQ_SCHEMA = "her30.dexter.request/1"
RES_SCHEMA = "her30.dexter.result/1"
# Environment passed to the child: nothing is inherited except these, plus what the caller
# explicitly lists in `provider_env` (the one model's key / base URL).
_BASE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
HARD_LIMITS = {"wall_seconds": 600, "max_iterations": 8, "max_total_tokens": 100_000, "max_tool_calls": 20}


def verify_pack(path: Path) -> dict:
    pack = json.loads(path.read_text(encoding="utf-8"))
    if pack.get("schema") != "her30.evidence/1":
        raise ValueError("evidence pack has the wrong schema")
    expect = snapshot_hash(pack["items"], pack["security_id"], pack["as_of"])
    if expect != pack.get("snapshot_id"):
        raise ValueError("evidence pack hash mismatch (modified after it was built)")
    return pack


def _failed(job_id: str, attempt_id: str, reason: str, detail: str, started: float) -> dict[str, Any]:
    return {
        "schema": RES_SCHEMA, "job_id": job_id, "attempt_id": attempt_id, "status": "failed", "reason": reason,
        "findings": [], "unknowns": [detail], "answer_markdown": "", "tool_calls": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "reasoning_tokens": None, "cached_tokens": None,
                  "measured": False, "provider_reported": False},
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def run_dexter_job(
    *,
    evidence_pack_path: Path,
    question: str,
    model: dict[str, str],
    jobs_root: Path,
    dexter_root: Path,
    provider_env: dict[str, str] | None = None,
    market: str = "TW",
    report_language: str = "zh-TW",
    limits: dict[str, float] | None = None,
    bun: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    job_id = job_id or uuid.uuid4().hex[:16]
    attempt_id = uuid.uuid4().hex[:16]
    bun = bun or os.environ.get("TAL_BUN") or shutil.which("bun")
    if not bun or not (dexter_root / "src" / "agent" / "agent.ts").exists():
        return _failed(job_id, attempt_id, "runner_unavailable", "bun or the Dexter checkout was not found", started)
    try:
        pack = verify_pack(evidence_pack_path)
    except Exception as exc:  # noqa: BLE001
        return _failed(job_id, attempt_id, "bad_evidence_pack", f"{type(exc).__name__}: {exc}", started)

    lim = {k: min(float((limits or {}).get(k, v)), v) for k, v in HARD_LIMITS.items()}
    job_dir = (jobs_root / job_id).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    request = {
        "schema": REQ_SCHEMA, "job_id": job_id, "attempt_id": attempt_id, "security_id": pack["security_id"], "market": market,
        "as_of": pack["as_of"], "question": question[:2000], "report_language": report_language, "model": model,
        "evidence_pack_path": str(evidence_pack_path.resolve()), "limits": lim,
    }
    env = {k: os.environ[k] for k in _BASE_ENV if k in os.environ}
    env.update({
        "HOME": str(job_dir), "DEXTER_ROOT": str(dexter_root), "TAL_DEXTER_JOB_DIR": str(job_dir),
        "NO_COLOR": "1", "CI": "1",
    })
    env.update({k: v for k, v in (provider_env or {}).items()})
    proc = subprocess.Popen(
        [bun, "run", str(RUNNER_DIR / "runner.ts")], cwd=str(job_dir), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(json.dumps(request) + "\n", timeout=lim["wall_seconds"] + 20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        res = _failed(job_id, attempt_id, "runner_hung", "the runner did not exit; killed", started)
        res["status"] = "timeout"
        return res
    last = [ln for ln in out.splitlines() if ln.strip()][-1:] or [""]
    try:
        res = json.loads(last[0])
    except ValueError:
        return _failed(job_id, attempt_id, "bad_runner_output", f"exit {proc.returncode}, stdout was not JSON", started)
    if not isinstance(res, dict) or res.get("schema") != RES_SCHEMA or res.get("status") not in (
        "succeeded", "partial", "failed", "timeout", "canceled", "unsupported"
    ):
        return _failed(job_id, attempt_id, "bad_runner_output", "result did not match the schema", started)
    if res["status"] == "succeeded" and not any(f.get("verification") == "verified" for f in res.get("findings", [])):
        res["status"], res["reason"] = "partial", "no_verified_finding"   # never trust a bare "succeeded"
    res.setdefault("job_id", job_id)
    res["job_dir"] = str(job_dir)
    return res
