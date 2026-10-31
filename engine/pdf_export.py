"""On-demand PDF export of a report HTML document.

The page builds the report HTML (the same document the "open HTML" action produces: zh-TW text, cost report, evidence and
disclaimers already inside) and POSTs it here ONLY when the user clicks "Export PDF". Nothing is rendered when a report
completes and nothing at import or startup: this module spawns a browser process per request and exits it again.

Renderer: the machine's own headless Chrome/Chromium `--print-to-pdf` (real text, selectable CJK, links, table pagination).
No dependency is added and no document or text ever leaves the machine. Hardening, because the input is HTML from the page:
  * the input is a document string, never a URL or path; it is written to a private temp dir and loaded as file://;
  * a CSP meta (`default-src 'none'`, inline styles and data: images only) is injected, so scripts and fetches cannot run, and
    the browser also runs with every host name unresolvable;
  * fresh throw-away profile, allow-listed environment (no engine secrets), hard timeout, process group killed on expiry;
  * size cap on the document and on the produced PDF, and one render at a time (a second request is refused as busy).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024
RENDER_TIMEOUT_S = 60

_CHROME_CANDIDATES_MAC = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")

_slot = threading.Lock()
_ENV_ALLOW = frozenset({"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "__CF_USER_TEXT_ENCODING"})

CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:\">"
)
# Pagination rules for paper: no rows split across pages, headings stay with their text, long URLs wrap, code wraps.
PRINT_CSS = (
    "<style>@page{size:A4;margin:14mm 12mm}"
    "@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}"
    "tr,img,blockquote{break-inside:avoid}thead{display:table-header-group}"
    "h1,h2,h3,h4{break-after:avoid}a,pre,code{overflow-wrap:anywhere}td,p,li{overflow-wrap:break-word}pre,code{white-space:pre-wrap}"
    "table{max-width:100%}}</style>"
)


class PdfExportError(Exception):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


def find_chrome() -> Optional[str]:
    env = os.environ.get("TAL_PDF_CHROME")
    if env and os.access(env, os.X_OK):
        return env
    for p in _CHROME_CANDIDATES_MAC:
        if os.access(p, os.X_OK):
            return p
    for n in _CHROME_NAMES:
        found = shutil.which(n)
        if found:
            return found
    return None


def prepare_html(html: str) -> str:
    """Inject the CSP and print CSS right after <head>; refuse documents that carry active content."""
    if re.search(r"<\s*(script|iframe|object|embed|link|base)\b", html, re.I):
        raise PdfExportError("pdf_bad_document", 400, "document contains active or external content")
    m = re.search(r"<head[^>]*>", html, re.I)
    inject = CSP_META + PRINT_CSS
    if m:
        return html[: m.end()] + inject + html[m.end():]
    return f"<!DOCTYPE html><html><head><meta charset=\"utf-8\">{inject}</head><body>{html}</body></html>"


def render_pdf(html: str) -> bytes:
    """Render `html` to PDF bytes. Raises PdfExportError (code/status) for every expected failure."""
    if not isinstance(html, str) or not html.strip():
        raise PdfExportError("pdf_empty", 400, "document is empty")
    data = html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        raise PdfExportError("pdf_too_large", 413, "document too large")
    chrome = find_chrome()
    if chrome is None:
        raise PdfExportError("pdf_unavailable", 503, "no Chrome/Chromium found on the server (set TAL_PDF_CHROME)")
    doc = prepare_html(html).encode("utf-8")
    if not _slot.acquire(blocking=False):
        raise PdfExportError("pdf_busy", 429, "another PDF is being produced; retry in a moment")
    try:
        with tempfile.TemporaryDirectory(prefix="tal-pdf-") as tmp:
            root = Path(tmp)
            src, out, prof = root / "report.html", root / "report.pdf", root / "profile"
            src.write_bytes(doc)
            cmd = [
                chrome, "--headless=new", "--disable-gpu", "--disable-extensions", "--no-first-run",
                "--no-default-browser-check", "--disable-background-networking", "--no-pdf-header-footer",
                "--host-resolver-rules=MAP * ~NOTFOUND", f"--user-data-dir={prof}",
                f"--print-to-pdf={out}", src.as_uri(),
            ]
            # Allow-list, not a copy of the engine's environment (no tokens/keys). HOME stays the real one: a temp HOME makes
            # Chrome on macOS stall ~10 s on first launch (measured), the profile itself is the throw-away dir above.
            env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
            env.setdefault("PATH", "/usr/bin:/bin")
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                proc.wait(timeout=RENDER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _kill(proc)
                raise PdfExportError("pdf_timeout", 504, "PDF rendering timed out") from None
            finally:
                if proc.poll() is None:
                    _kill(proc)
            if not out.is_file():
                raise PdfExportError("pdf_failed", 500, "PDF rendering failed")
            if out.stat().st_size > MAX_PDF_BYTES:
                raise PdfExportError("pdf_too_large", 413, "PDF too large")
            pdf = out.read_bytes()
            if not pdf.startswith(b"%PDF-"):
                raise PdfExportError("pdf_failed", 500, "PDF rendering failed")
            return pdf
    finally:
        _slot.release()


def _kill(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def safe_filename(name: str, default: str = "report") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "")).strip("-.")[:64] or default
    return f"{base}.pdf"
