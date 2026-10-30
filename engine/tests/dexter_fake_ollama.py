"""A scripted Ollama-compatible HTTP server (tests only). It stands in for the MODEL so the
real Dexter agent loop and the real read_file tool run unchanged."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tool_call(name: str, args: dict) -> dict:
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def text(content: str) -> dict:
    return {"message": {"role": "assistant", "content": content}}


class FakeOllama:
    def __init__(self, script, *, delay: float = 0.0, status: int = 200):
        self.script, self.delay, self.status = script, delay, status
        self.requests: list[dict] = []
        self.turn = 0
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):  # noqa: N802
                self._send(200, {"models": []})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                me.requests.append(body)
                if me.delay:
                    time.sleep(me.delay)
                if me.status != 200:
                    return self._send(me.status, {"error": "boom"})
                step = me.script(me.turn, body) if callable(me.script) else me.script[min(me.turn, len(me.script) - 1)]
                me.turn += 1
                resp = {"model": body.get("model", "m"), "created_at": "2026-10-30T00:00:00Z", "done": True, "done_reason": "stop",
                        "prompt_eval_count": 100, "eval_count": 20, **step}
                if body.get("stream", True):
                    raw = (json.dumps(resp) + "\n").encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/x-ndjson")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self._send(200, resp)

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
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
