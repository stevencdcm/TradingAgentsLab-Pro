"""Test-only stand-in for the workbench proxy: strips /lab-pro, strips any client x-tal-* headers, adds the hop headers."""
import asyncio, os, sys
import httpx, websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
import uvicorn

UP = "127.0.0.1:" + sys.argv[1]; PUBLIC_HOST = "127.0.0.1:" + sys.argv[2]
SECRET = open(sys.argv[3]).read().strip(); OWNER = sys.argv[4]
client = httpx.AsyncClient(base_url="http://" + UP, follow_redirects=False, timeout=None)

def hdrs(h):
    out = {k: v for k, v in h if k.lower() not in ("host", "content-length", "connection") and not k.lower().startswith("x-tal-")}
    out["host"] = PUBLIC_HOST; out["x-tal-proxy-auth"] = SECRET; out["x-tal-owner"] = OWNER
    return out

async def http(request: Request):
    path = request.path_params.get("path", "")
    if request.url.path == "/lab-pro": return RedirectResponse("/lab-pro/")
    r = await client.request(request.method, "/" + path, params=request.query_params, headers=hdrs(request.headers.items()), content=await request.body())
    h = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding", "content-encoding")}
    resp = Response(r.content, r.status_code)
    for k, v in r.headers.multi_items():
        if k.lower() == "set-cookie": resp.headers.append("set-cookie", v)
        elif k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "set-cookie"): resp.headers[k] = v
    return resp

async def ws_route(ws: WebSocket):
    path = ws.path_params.get("path", "")
    await ws.accept()
    extra = hdrs([(k, v) for k, v in ws.headers.items() if k.lower() in ("origin", "cookie")])
    extra.pop("host", None)
    extra["Host"] = PUBLIC_HOST
    qs = ("?" + ws.url.query) if ws.url.query else ""
    try:
        async with websockets.connect(f"ws://{UP}/{path}{qs}", additional_headers=extra, max_size=None, ping_interval=None, open_timeout=10) as up:
            async def a():
                async for m in ws.iter_text(): await up.send(m)
            async def b():
                async for m in up: await ws.send_text(m if isinstance(m, str) else m.decode())
            await asyncio.wait([asyncio.create_task(a()), asyncio.create_task(b())], return_when=asyncio.FIRST_COMPLETED)
    except Exception as e:
        print("ws upstream failed", type(e).__name__, str(e)[:100], flush=True)
    try: await ws.close()
    except Exception: pass

app = Starlette(routes=[Route("/lab-pro", http, methods=["GET"]), Route("/lab-pro/{path:path}", http, methods=["GET","POST","PUT","DELETE","PATCH"]), WebSocketRoute("/lab-pro/{path:path}", ws_route)])
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
