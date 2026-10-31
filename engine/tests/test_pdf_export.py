"""On-demand report PDF: hardening of the document that reaches the browser, request guards, busy/timeout/unavailable
handling, and one real headless-Chrome render of a CJK report (skipped where no Chrome exists). No model, no network."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from engine import pdf_export, secret_refs
from engine.pdf_export import PdfExportError
from engine.secret_store import SecretStore
from engine.web_app import COOKIE_NAME, build_web_app
from engine.tests.test_web_app import BASE, COOKIE, LAUNCH, PORT, MemKeyring

DOC = ('<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8"><title>2330 · 2026-10-31</title>'
       '<style>body{font-family:sans-serif}table{border-collapse:collapse}td,th{border:1px solid #888}</style></head><body>'
       '<h1>台積電 2330 分析報告</h1><p>營收年增，毛利率維持高檔。API 價格估算：$0.0201</p>'
       '<table><thead><tr><th>指標</th><th>數值</th></tr></thead><tbody>' + ''.join(
           f'<tr><td>RSI{i}</td><td>46.{i}</td></tr>' for i in range(80)) + '</tbody></table>'
       '<p><a href="https://example.com/a">來源</a></p></body></html>')
HAVE_CHROME = pdf_export.find_chrome() is not None


@pytest.fixture
def web(tmp_path, tmp_db):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html><head><title>x</title></head><body>app</body></html>")
    app = build_web_app(token="engine-token", port=PORT, data_dir=tmp_path / "data", static_dir=static,
                        launch_secret=LAUNCH, cookie_value=COOKIE, store=SecretStore(MemKeyring()))
    with TestClient(app, base_url=BASE) as c:
        c.cookies.set(COOKIE_NAME, COOKIE)
        yield c
    secret_refs.configure(None)


def test_rebuilt_document_starts_with_csp_and_keeps_report_content():
    out = pdf_export.prepare_html(DOC)
    assert out.startswith("<!DOCTYPE html>")
    assert out.index("Content-Security-Policy") < out.index("<title>") < out.index("<body>")
    assert out.count("Content-Security-Policy") == 1 and "@page" in out
    assert "台積電 2330 分析報告" in out and "$0.0201" in out and 'href="https://example.com/a"' in out
    assert out.count("<tr>") == 81 and "<thead>" in out                      # table structure survives


@pytest.mark.parametrize("bad", [
    "<script>1</script>", "<SCRIPT>x</SCRIPT>", "<iframe src=x></iframe>", '<link rel=stylesheet href="http://x/a.css">',
    "<base href=file:///>", "<object data=x>", "<embed src=x>", "<svg><image href=http://x/a.png /></svg>", "<math></math>",
    "<video src=http://x/v></video>", "<audio src=x></audio>", "<video><source src=x></video>", "<frameset><frame src=file:///etc/hosts></frameset>",
    '<meta http-equiv="refresh" content="0;url=http://x/">', "<meta http-equiv=refresh content='0;url=file:///etc/hosts'>",
    '<div onclick="x()">a</div>', '<img src=data:image/png;base64,AAAA onerror=x()>',
    '<div style="background:url(http://x/a.png)">a</div>', "<style>@import 'http://x/a.css';</style>", "<style>a{background:url(//x/a.png)}</style>",
    "<style>a{b:\\75rl(http://x/a)}</style>", "<canvas></canvas>", "<template><img src=x></template>", "<noscript><img src=x></noscript>",
])
def test_active_navigating_or_fetching_content_is_refused(bad):
    for wrap in ("<html><head></head><body>{}</body></html>", "<body>{}</body>", "{}", "<header>{}</header>"):
        with pytest.raises(PdfExportError) as e:
            pdf_export.prepare_html(wrap.format(bad))
        assert e.value.code == "pdf_bad_document" and e.value.status == 400


def test_header_tag_and_comment_tricks_cannot_move_the_csp_or_smuggle_a_fetch():
    for tricky in ('<header><img src="http://x/h.png"></header>', '<!--<head>--><img src="http://x/c.png">',
                   '<p>hi</p><head><img src="http://x/l.png"></head>', '<img src="file:///etc/hosts">', '<img src="//x/p.png">',
                   '<a href="javascript:alert(1)">x</a>', '<a href="file:///etc/hosts">x</a>', '<img src=" http://x/y.png">'):
        out = pdf_export.prepare_html(f"<html><head></head><body>{tricky}</body></html>")
        assert out.index("Content-Security-Policy") < out.index("<body>")
        assert "http://x/" not in out.replace("https://example.com", "") and "file:" not in out and "javascript:" not in out
        assert "<!--" not in out and "//x/p.png" not in out
        assert out.count("Content-Security-Policy") == 1


def test_data_images_text_escaping_and_task_list_inputs():
    out = pdf_export.prepare_html('<body><p>a &lt;b&gt; &amp; c</p><input type=checkbox disabled> done'
                                  '<img src="data:image/png;base64,iVBORw0KGgo=" alt="x"></body>')
    assert "a &lt;b&gt; &amp; c" in out and "<input" not in out and 'src="data:image/png;base64,iVBORw0KGgo="' in out


def test_filename_is_safe_and_empty_falls_back():
    assert pdf_export.safe_filename("2330-2026-10-31") == "2330-2026-10-31.pdf"
    assert pdf_export.safe_filename('../../etc/passwd"; x') == "etc-passwd-x.pdf"
    assert pdf_export.safe_filename("") == "report.pdf" and pdf_export.safe_filename(None) == "report.pdf"
    assert pdf_export.safe_filename("台積電") == "report.pdf"


def test_empty_oversized_and_no_chrome_are_clean_errors(monkeypatch):
    for bad in (None, "", "   ", 5):
        with pytest.raises(PdfExportError) as e:
            pdf_export.render_pdf(bad)  # type: ignore[arg-type]
        assert e.value.code == "pdf_empty"
    with pytest.raises(PdfExportError) as e:
        pdf_export.render_pdf("x" * (pdf_export.MAX_HTML_BYTES + 1))
    assert e.value.status == 413
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: None)
    with pytest.raises(PdfExportError) as e:
        pdf_export.render_pdf(DOC)
    assert (e.value.code, e.value.status) == ("pdf_unavailable", 503)


def test_second_render_while_one_is_running_is_busy_not_queued(monkeypatch):
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: "/bin/true")
    assert pdf_export._slot.acquire(blocking=False)
    try:
        with pytest.raises(PdfExportError) as e:
            pdf_export.render_pdf(DOC)
        assert (e.value.code, e.value.status) == ("pdf_busy", 429)
    finally:
        pdf_export._slot.release()


def test_timeout_kills_the_browser_and_releases_the_slot(monkeypatch):
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: "/bin/sleep")   # `sleep <args>` never writes a PDF
    monkeypatch.setattr(pdf_export, "RENDER_TIMEOUT_S", 0.3)
    import subprocess, time
    real = subprocess.Popen
    started: list = []
    def popen(cmd, **kw):
        p = real(["/bin/sleep", "30"], **{k: v for k, v in kw.items()})
        started.append(p); return p
    monkeypatch.setattr(pdf_export.subprocess, "Popen", popen)
    t0 = time.time()
    with pytest.raises(PdfExportError) as e:
        pdf_export.render_pdf(DOC)
    assert e.value.code == "pdf_timeout" and time.time() - t0 < 10
    assert started[0].poll() is not None                                   # really killed
    assert pdf_export._slot.acquire(blocking=False); pdf_export._slot.release()


def test_failed_render_without_output_is_an_error(monkeypatch):
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: "/usr/bin/true")
    with pytest.raises(PdfExportError) as e:
        pdf_export.render_pdf(DOC)
    assert e.value.code == "pdf_failed"


@pytest.mark.skipif(not HAVE_CHROME, reason="no Chrome/Chromium on this machine")
def test_real_render_produces_a_multi_page_pdf_with_cjk_text(tmp_path):
    pdf = pdf_export.render_pdf(DOC)
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 10_000                                             # 80 table rows + CJK font subset
    assert not list(tmp_path.glob("tal-pdf-*"))


def test_route_requires_the_launch_cookie(web):
    anon = TestClient(web.app, base_url=BASE)
    assert anon.post("/web/report-pdf", json={"html": DOC}).status_code == 403


def test_route_returns_attachment_and_maps_errors(web, monkeypatch):
    monkeypatch.setattr(pdf_export, "render_pdf", lambda html, cancel=None: b"%PDF-1.4 fake")
    r = web.post("/web/report-pdf", json={"html": DOC, "name": "2330-2026-10-31"})
    assert r.status_code == 200 and r.content == b"%PDF-1.4 fake"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="2330-2026-10-31.pdf"'
    assert r.headers["cache-control"] == "no-store"

    def boom(html, cancel=None):
        raise PdfExportError("pdf_busy", 429, "another PDF is being produced; retry in a moment")
    monkeypatch.setattr(pdf_export, "render_pdf", boom)
    r = web.post("/web/report-pdf", json={"html": DOC})
    assert r.status_code == 429 and r.json() == {"ok": False, "code": "pdf_busy", "error": "another PDF is being produced; retry in a moment"}


def test_nothing_renders_unless_the_route_is_called(web, monkeypatch):
    calls = []
    monkeypatch.setattr(pdf_export, "render_pdf", lambda html, cancel=None: calls.append(1) or b"%PDF-")
    web.get("/web/info"); web.get("/")
    assert calls == []


# ---- network / file isolation with a REAL browser, layers tested independently -------------------------------------

import asyncio, os, socket, time, glob, tempfile
from engine.tests.test_web_app import PORT as _PORT


class _Listener:
    """A loopback listener standing in for 'the network' / an internal service: any connection is a leak."""

    def __init__(self):
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(16); self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]; self.hits = 0; self._stop = False
        self.t = threading.Thread(target=self._run, daemon=True); self.t.start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept(); self.hits += 1; c.close()
            except (socket.timeout, OSError):
                pass

    def close(self):
        self._stop = True; self.t.join(2); self.sock.close()


def _no_network_flags(monkeypatch):
    """Remove the browser-level network layers (resolver map, dead proxy): only document-level defences remain."""
    orig = pdf_export._chrome_args
    monkeypatch.setattr(pdf_export, "_chrome_args", lambda p, o: [a for a in orig(p, o) if not a.startswith(("--host-resolver-rules", "--proxy-"))])


def _fixtures(port: int) -> dict[str, str]:
    u = f"http://127.0.0.1:{port}"
    return {
        "header-img": f'<header><img src="{u}/header"></header><p>x</p>',
        "comment-head": f'<!--<head>--><img src="{u}/comment"><p>x</p>',
        "late-head": f'<p>x</p><head><link rel=stylesheet href="{u}/late.css"></head><img src="{u}/late">',
        "img-attr-tricks": f'<img src="{u}/a" srcset="{u}/b 2x"><img src=" {u}/c"><img src="//127.0.0.1:{port}/d">',
        "css-url": f'<style>body{{background:url({u}/css)}}</style><p style="background:url({u}/inline)">x</p>',
        "a-click-targets": f'<a href="{u}/link">l</a><a href="javascript:fetch(\'{u}/js\')">j</a>',
        "video": f'<video src="{u}/video" autoplay></video>',
        "meta-refresh": f'<meta http-equiv="refresh" content="0;url={u}/refresh"><p>x</p>',
        "frameset-file": '<frameset><frame src="file:///etc/hosts"></frameset>',
        "iframe": f'<iframe src="{u}/frame"></iframe>',
        "svg-image": f'<svg xmlns="http://www.w3.org/2000/svg"><image href="{u}/svg"/></svg>',
    }


@pytest.mark.skipif(not HAVE_CHROME, reason="no Chrome/Chromium on this machine")
def test_malicious_documents_make_no_connection_even_without_resolver_or_proxy_flags(monkeypatch):
    """Full pipeline, browser network flags REMOVED: every fixture is either refused (400) or rendered with zero fetches."""
    _no_network_flags(monkeypatch)
    lst = _Listener()
    try:
        outcomes = {}
        for name, frag in _fixtures(lst.port).items():
            try:
                pdf_export.render_pdf(f"<html><head><title>t</title></head><body>{frag}</body></html>")
                outcomes[name] = "rendered"
            except PdfExportError as e:
                outcomes[name] = e.code
        time.sleep(0.5)
        assert lst.hits == 0, (lst.hits, outcomes)
        assert outcomes["header-img"] == outcomes["comment-head"] == outcomes["img-attr-tricks"] == "rendered"
        assert outcomes["a-click-targets"] == "rendered"
        for name in ("late-head", "css-url", "video", "meta-refresh", "frameset-file", "iframe", "svg-image"):
            assert outcomes[name] == "pdf_bad_document", (name, outcomes)
    finally:
        lst.close()


@pytest.mark.skipif(not HAVE_CHROME, reason="no Chrome/Chromium on this machine")
def test_csp_layer_alone_blocks_fetches_even_when_the_markup_is_not_sanitised(monkeypatch):
    """Sanitiser bypassed AND network flags removed: the CSP, placed first in the document, still stops the document-level
    fetches that the old head-regex placement let through (header / comment / late head / video). Navigations (meta refresh, iframe/frame:
    measured: Chrome still opens a connection for an iframe despite CSP) are not something CSP reliably governs; those are closed by
    the sanitiser and the browser network flags, each tested separately."""
    _no_network_flags(monkeypatch)
    monkeypatch.setattr(pdf_export, "prepare_html", lambda h: "<!DOCTYPE html><html><head>" + pdf_export.CSP_META + "</head><body>" + h + "</body></html>")
    lst = _Listener()
    try:
        fx = _fixtures(lst.port)
        for name in ("header-img", "comment-head", "late-head", "img-attr-tricks", "css-url", "video", "svg-image"):
            pdf_export.render_pdf(f"<html><head><title>t</title></head><body>{fx[name]}</body></html>")
        time.sleep(0.5)
        assert lst.hits == 0
    finally:
        lst.close()


@pytest.mark.skipif(not HAVE_CHROME, reason="no Chrome/Chromium on this machine")
def test_control_without_any_layer_does_connect(monkeypatch):
    """Guards the guard: with no sanitiser, no CSP and no browser flags the same fixture really reaches the listener, so the zero
    counts above mean something."""
    _no_network_flags(monkeypatch)
    monkeypatch.setattr(pdf_export, "prepare_html", lambda h: "<!DOCTYPE html><html><body>" + h + "</body></html>")
    lst = _Listener()
    try:
        pdf_export.render_pdf(f'<img src="http://127.0.0.1:{lst.port}/c">')
        time.sleep(0.5)
        assert lst.hits >= 1
    finally:
        lst.close()


# ---- bounded request size ---------------------------------------------------------------------------------------------

def _scope(path="/web/report-pdf", extra=()):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "server": ("127.0.0.1", _PORT), "client": ("127.0.0.1", 5555),
            "headers": [(b"host", f"127.0.0.1:{_PORT}".encode()), (b"cookie", f"{COOKIE_NAME}={COOKIE}".encode()),
                        (b"content-type", b"application/json"), *extra]}


async def _call(app, scope, receive):
    sent = []
    async def send(m): sent.append(m)
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


def test_declared_oversize_is_refused_before_reading(web, monkeypatch):
    monkeypatch.setattr(pdf_export, "render_pdf", lambda *a, **k: pytest.fail("must not render"))
    reads = []
    async def receive():
        reads.append(1); return {"type": "http.request", "body": b"x", "more_body": False}
    st, body = asyncio.run(_call(web.app, _scope(extra=[(b"content-length", str(pdf_export.MAX_BODY_BYTES + 1).encode())]), receive))
    assert st == 413 and b"pdf_too_large" in body and reads == []


def test_streamed_body_over_the_cap_stops_reading_even_with_a_lying_or_missing_length(web, monkeypatch):
    monkeypatch.setattr(pdf_export, "render_pdf", lambda *a, **k: pytest.fail("must not render"))
    for extra in ([], [(b"content-length", b"10")]):          # chunked / lying small Content-Length
        chunk = b"a" * (1024 * 1024)
        reads = []
        async def receive():
            reads.append(1)
            return {"type": "http.request", "body": chunk, "more_body": True}
        st, body = asyncio.run(_call(web.app, _scope(extra=extra), receive))
        assert st == 413 and b"pdf_too_large" in body
        assert len(reads) <= pdf_export.MAX_BODY_BYTES // len(chunk) + 2          # stopped right after the cap, not at EOF


def test_bad_json_is_a_400_not_a_crash(web, monkeypatch):
    monkeypatch.setattr(pdf_export, "render_pdf", lambda *a, **k: pytest.fail("must not render"))
    r = web.post("/web/report-pdf", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "pdf_bad_request"
    r = web.post("/web/report-pdf", json=[1, 2])
    assert r.status_code == 400


# ---- cancel / disconnect really stops the browser ---------------------------------------------------------------------

def _fake_chrome(tmp_path):
    pidfile = tmp_path / "chrome.pid"
    script = tmp_path / "fake-chrome.sh"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 60 &\necho $! >> {pidfile}\nwait\n")
    script.chmod(0o755)
    return script, pidfile


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0); return True
    except ProcessLookupError:
        return False


def _tmp_dirs():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "tal-pdf-*")))


def _wait_pids(pidfile, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pidfile.exists() and len(pidfile.read_text().split()) >= 2:
            return [int(x) for x in pidfile.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("fake chrome did not start")


def _gone(pids, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    return False


def test_cancel_event_kills_the_whole_process_group_frees_the_slot_and_the_temp_dir(monkeypatch, tmp_path):
    script, pidfile = _fake_chrome(tmp_path)
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: str(script))
    before = _tmp_dirs()
    cancel = threading.Event()
    threading.Timer(0.6, cancel.set).start()
    t0 = time.time()
    with pytest.raises(PdfExportError) as e:
        pdf_export.render_pdf(DOC, cancel)
    assert e.value.code == "pdf_cancelled" and time.time() - t0 < 5
    pids = _wait_pids(pidfile)
    assert _gone(pids), "shell and its child must both be dead"
    assert _tmp_dirs() == before
    assert pdf_export._slot.acquire(blocking=False); pdf_export._slot.release()


def test_client_disconnect_stops_the_browser_and_the_next_export_is_not_busy(web, monkeypatch, tmp_path):
    script, pidfile = _fake_chrome(tmp_path)
    monkeypatch.setattr(pdf_export, "find_chrome", lambda: str(script))
    before = _tmp_dirs()
    body = json.dumps({"html": DOC}).encode()

    async def run():
        state = {"sent": False}
        gate = asyncio.Event()
        async def receive():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gate.wait()
            return {"type": "http.disconnect"}
        task = asyncio.create_task(_call(web.app, _scope(), receive))
        pids = await asyncio.get_running_loop().run_in_executor(None, _wait_pids, pidfile)
        assert all(_alive(p) for p in pids)                       # rendering is genuinely in progress
        gate.set()                                                # the browser tab / fetch goes away
        await asyncio.wait_for(task, 10)
        return pids

    pids = asyncio.run(run())
    assert _gone(pids), "disconnect must kill the Chrome process group"
    assert _tmp_dirs() == before
    assert pdf_export._slot.acquire(blocking=False); pdf_export._slot.release()
