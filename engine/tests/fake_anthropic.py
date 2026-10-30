"""Fake Anthropic Messages API server (tests only): captures the JSON body of every request so the
REAL SDK's request layer is exercised end to end (the SDK raises on unknown kwargs client-side)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAnthropic:
    def __init__(self, key: str = "sk-ant-test-key-1234567890"):
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                me.bodies.append(body)
                me.headers.append({k.lower(): v for k, v in self.headers.items()})
                ok = key in (self.headers.get("x-api-key"), (self.headers.get("authorization") or "").removeprefix("Bearer "))
                tool = [t for t in body.get("tools", [])]
                content = ([{"type": "tool_use", "id": "toolu_1", "name": tool[0]["name"], "input": {}}] if tool and body.get("tool_choice")
                           else [{"type": "text", "text": "好的，這是回覆。"}])
                resp = {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"), "content": content,
                        "stop_reason": "tool_use" if content[0]["type"] == "tool_use" else "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 12, "output_tokens": 7}}
                raw = json.dumps(resp if ok else {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
