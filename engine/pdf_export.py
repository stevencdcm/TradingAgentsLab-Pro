"""On-demand PDF export of a report HTML document.

The page builds the report HTML (the same document the "open HTML" action produces: zh-TW text, cost report, evidence and
disclaimers already inside) and POSTs it here ONLY when the user clicks "Export PDF". Nothing is rendered when a report
completes and nothing at import or startup: this module spawns a browser process per request and exits it again.

Renderer: the machine's own headless Chrome/Chromium `--print-to-pdf` (real text, selectable CJK, links, table pagination).
No dependency is added and no document or text ever leaves the machine. Hardening, because the input is HTML from the page:
  * the input is a document string, never a URL or path; it is NOT passed to the browser as written: it is parsed and
    re-serialized from an allow-list of tags and attributes (no scripts, frames, meta refresh, links, svg, media, remote
    images or url() in CSS; unsafe constructs reject the whole document), so the browser only ever sees our own markup;
  * the rebuilt document starts with a CSP meta (`default-src 'none'`, inline styles and data: images only) as its first
    element, and the browser additionally runs with unresolvable host names and an unreachable proxy for everything,
    including loopback. Each layer is tested on its own with the other layers removed;
  * fresh throw-away profile, allow-listed environment (no engine secrets), hard timeout, process group killed on expiry;
  * size cap on the document and on the produced PDF, and one render at a time (a second request is refused as busy).
"""
from __future__ import annotations

import html as _html
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

MAX_HTML_BYTES = 8 * 1024 * 1024
# Transport cap on the request body: the 8 MB document plus JSON escaping (quotes, newlines) headroom.
MAX_BODY_BYTES = 12 * 1024 * 1024
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
    "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-src 'none'; child-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\">"
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


_VOID = frozenset({"br", "hr", "img", "col", "wbr"})
_ALLOWED_TAGS = frozenset({
    "div", "span", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead",
    "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col", "a", "strong", "b", "em", "i", "u", "s", "del", "ins", "sub",
    "sup", "code", "pre", "blockquote", "small", "mark", "details", "summary", "header", "footer", "section", "article", "aside",
    "nav", "main", "figure", "figcaption", "abbr", "kbd", "img", "wbr",
})
# Present anywhere => the document is refused (never silently stripped): active, navigating or fetching content.
# Other unlisted tags (form controls such as GFM task-list checkboxes, unknown elements) are simply dropped.
_REJECT_TAGS = frozenset({
    "script", "iframe", "frame", "frameset", "object", "embed", "applet", "link", "base", "svg", "math", "video", "audio", "source",
    "track", "portal", "fencedframe", "canvas", "noscript", "template",
})
_ATTR_OK = frozenset({"class", "id", "lang", "dir", "title", "colspan", "rowspan", "align", "open", "start", "alt", "width", "height"})
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")
_CSS_BAD = re.compile(r"@import|url\s*\(|expression\s*\(|behavior\s*:|-moz-binding|image-set|src\s*\(|\\|<", re.I)
_HREF_OK = re.compile(r"^(https?:|mailto:|#)", re.I)


def _bad(msg: str) -> PdfExportError:
    return PdfExportError("pdf_bad_document", 400, msg)


class _Rebuilder(HTMLParser):
    """Re-serializes only allow-listed markup. Everything else is dropped; dangerous constructs raise."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.head: list[str] = []
        self.title = ""
        self.lang = "en"
        self._in_title = False
        self._in_style = False
        self._style: list[str] = []

    def _attrs(self, tag: str, attrs) -> str:
        out = []
        for name, value in attrs:
            n = (name or "").lower()
            v = "" if value is None else value
            if n.startswith("on"):
                raise _bad("event handler attribute")
            if n in _ATTR_OK or n.startswith("data-"):
                out.append(f' {n}="{_html.escape(v)}"')
            elif n == "style":
                if _CSS_BAD.search(v):
                    raise _bad("unsafe inline style")
                out.append(f' style="{_html.escape(v)}"')
            elif n == "href" and tag == "a":
                if _HREF_OK.match(v.strip()):
                    out.append(f' href="{_html.escape(v.strip())}"')
            elif n == "src" and tag == "img":
                if re.match(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$", v.strip(), re.I):
                    out.append(f' src="{_html.escape(v.strip())}"')
                else:
                    return None  # type: ignore[return-value]  # remote / file image: drop the element
        return "".join(out)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _REJECT_TAGS:
            raise _bad(f"<{tag}> is not allowed")
        if tag == "meta":
            d = {(k or "").lower(): (v or "") for k, v in attrs}
            if "http-equiv" in d or "refresh" in d.get("content", "").lower():
                raise _bad("meta http-equiv/refresh is not allowed")
            return                                  # every other meta is dropped; we write our own charset/CSP
        if tag == "html":
            lang = dict((k.lower(), v) for k, v in attrs if k).get("lang") or ""
            if _LANG_RE.match(lang):
                self.lang = lang
            return
        if tag in ("head", "body"):
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "style":
            self._in_style = True
            self._style = []
            return
        if tag not in _ALLOWED_TAGS:
            return
        a = self._attrs(tag, attrs)
        if a is None:
            return
        self.body.append(f"<{tag}{a}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            css = "".join(self._style)
            if _CSS_BAD.search(css):
                raise _bad("unsafe CSS")
            self.head.append(f"<style>{css}</style>")
            self._in_style = False
        elif tag in _ALLOWED_TAGS and tag not in _VOID:
            self.body.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_style:
            self._style.append(data)
        elif self._in_title:
            self.title += data
        else:
            self.body.append(_html.escape(data, quote=False))

    # comments, doctype, CDATA, processing instructions: dropped
    def handle_comment(self, data): pass
    def handle_decl(self, decl): pass
    def handle_pi(self, data): pass
    def unknown_decl(self, data): pass


def prepare_html(html: str) -> str:
    """Rebuild the document from allow-listed markup, CSP first. Raises PdfExportError(400) for unsafe constructs."""
    rb = _Rebuilder()
    rb.feed(html)
    rb.close()
    title = _html.escape(rb.title.strip()[:200], quote=False)
    return (
        "<!DOCTYPE html>"
        f'<html lang="{rb.lang}"><head>{CSP_META}<meta charset="utf-8"><title>{title}</title>{PRINT_CSS}'
        f'{"".join(rb.head)}</head><body>{"".join(rb.body)}</body></html>'
    )


def render_pdf(html: str, cancel: Optional[threading.Event] = None) -> bytes:
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
            cmd = [chrome, *_chrome_args(prof, out), src.as_uri()]
            # Allow-list, not a copy of the engine's environment (no tokens/keys). HOME stays the real one: a temp HOME makes
            # Chrome on macOS stall ~10 s on first launch (measured), the profile itself is the throw-away dir above.
            env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
            env.setdefault("PATH", "/usr/bin:/bin")
            proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + RENDER_TIMEOUT_S
            try:
                while True:
                    try:
                        proc.wait(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel is not None and cancel.is_set():
                        _kill(proc)
                        raise PdfExportError("pdf_cancelled", 499, "PDF rendering was cancelled")
                    if time.monotonic() > deadline:
                        _kill(proc)
                        raise PdfExportError("pdf_timeout", 504, "PDF rendering timed out")
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


def _chrome_args(profile: Path, out: Path) -> list[str]:
    return [
        "--headless=new", "--disable-gpu", "--disable-extensions", "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--no-pdf-header-footer",
        # Network layers, independent of the CSP: names never resolve, and everything (loopback included) goes to a dead proxy.
        "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
        f"--user-data-dir={profile}", f"--print-to-pdf={out}",
    ]


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
