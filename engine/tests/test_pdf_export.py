"""On-demand report PDF: hardening of the document that reaches the browser, request guards, busy/timeout/unavailable
handling, and one real headless-Chrome render of a CJK report (skipped where no Chrome exists). No model, no network."""

from __future__ import annotations

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


def test_prepare_injects_csp_and_print_css_and_keeps_content():
    out = pdf_export.prepare_html(DOC)
    assert "default-src 'none'" in out and "@page" in out
    assert out.index("Content-Security-Policy") < out.index("<title>")
    assert "台積電 2330 分析報告" in out and "$0.0201" in out


@pytest.mark.parametrize("bad", ["<script>1</script>", "<iframe src=x></iframe>", '<link rel=stylesheet href="http://x/a.css">',
                                 "<base href=file:///>", "<object data=x>", "<embed src=x>", "<SCRIPT>x</SCRIPT>"])
def test_active_or_external_content_is_refused(bad):
    with pytest.raises(PdfExportError) as e:
        pdf_export.prepare_html(f"<html><head></head><body>{bad}</body></html>")
    assert e.value.code == "pdf_bad_document" and e.value.status == 400


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
    monkeypatch.setattr(pdf_export, "render_pdf", lambda html: b"%PDF-1.4 fake")
    r = web.post("/web/report-pdf", json={"html": DOC, "name": "2330-2026-10-31"})
    assert r.status_code == 200 and r.content == b"%PDF-1.4 fake"
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == 'attachment; filename="2330-2026-10-31.pdf"'
    assert r.headers["cache-control"] == "no-store"

    def boom(html):
        raise PdfExportError("pdf_busy", 429, "another PDF is being produced; retry in a moment")
    monkeypatch.setattr(pdf_export, "render_pdf", boom)
    r = web.post("/web/report-pdf", json={"html": DOC})
    assert r.status_code == 429 and r.json() == {"ok": False, "code": "pdf_busy", "error": "another PDF is being produced; retry in a moment"}


def test_nothing_renders_unless_the_route_is_called(web, monkeypatch):
    calls = []
    monkeypatch.setattr(pdf_export, "render_pdf", lambda html: calls.append(1) or b"%PDF-")
    web.get("/web/info"); web.get("/")
    assert calls == []
