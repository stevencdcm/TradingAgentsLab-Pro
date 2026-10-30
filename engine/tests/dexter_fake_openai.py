"""Scripted OpenAI-compatible server (tests only): plays the MODEL for both the real Dexter agent
loop (requests that carry tools) and the Lab Pro debate (requests without tools). Auth enforced."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "omlx-test-key-1234567890"


class FakeOpenAI:
    def __init__(self, dexter_answer: str, *, key: str = KEY):
        self.requests: list[dict] = []
        self.dexter_answer = dexter_answer
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                me.requests.append({"auth": self.headers.get("authorization"), "body": body})
                if self.headers.get("authorization") != f"Bearer {key}":
                    return self._json(401, {"error": {"message": "bad key"}})
                if body.get("tools"):
                    has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
                    if not has_tool_result:
                        msg = {"role": "assistant", "content": None, "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "evidence/pack.json"})}}]}
                        finish = "tool_calls"
                    else:
                        msg, finish = {"role": "assistant", "content": me.dexter_answer}, "stop"
                else:
                    system = (body["messages"][0].get("content") or "") if body["messages"] else ""
                    text = ("STANCE=MODERATELY_BULLISH\nCONVICTION=0.6\nBULL_STRENGTH=60\nBEAR_STRENGTH=40\nRISK=MODERATE\n依證據包 [price 2026-10-29] 判斷。"
                            if "portfolio manager" in system else "依證據包 [price 2026-10-29] 的分析。")
                    msg, finish = {"role": "assistant", "content": text}, "stop"
                usage = {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    delta = {k: v for k, v in msg.items() if k != "role" and v is not None}
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [{"index": 0, **tc} for tc in delta["tool_calls"]]
                    for chunk in (
                        {"id": "c", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                        {"id": "c", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage},
                    ):
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self._json(200, {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model"),
                                     "choices": [{"index": 0, "finish_reason": finish, "message": msg}], "usage": usage})

            def _json(self, code, obj):
                raw = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
