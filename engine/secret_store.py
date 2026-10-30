"""Server-side secret storage for the local web version.

Secrets live ONLY in the macOS Keychain (through the `keyring` library, which
talks to Security.framework directly — no secret is ever put on a command line,
in a file, in browser storage or in an API response). Fail closed: when no real
keychain backend is available, every write is refused instead of falling back to
a plaintext file.

The browser never receives a stored secret. It receives an opaque reference
``@@secret:<name>@@`` which the engine resolves server-side when it builds a
provider request (see ``engine/secret_refs.py``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Optional

SERVICE = "TradingAgentsLab-Web"
_INDEX = "__index__"
_OAUTH_FIELD = re.compile(r"^oauth:openai:(access|refresh)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,80}$")

# Plain configuration the UI must read back (not secrets).
PLAIN_CONFIG = frozenset({"local:base-url", "local:model"})
# Features that would need to hand secrets back to the page (URLs with embedded
# bot tokens, etc.). Not available in the web version.
BLOCKED_PREFIXES = ("webhooks:", "telegram:")


class SecretStoreError(Exception):
    """The store refused the operation (unavailable backend, bad name, blocked key)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ref_for(name: str) -> str:
    return f"@@secret:{name}@@"


REF_RE = re.compile(r"^@@secret:([A-Za-z0-9:_.\-]{1,80})@@$")


class SecretStore:
    def __init__(self, backend: Any | None = None) -> None:
        if backend is None:
            import keyring

            backend = keyring
        self._kr = backend
        self._lock = threading.Lock()

    # ---- availability -------------------------------------------------------

    def available(self) -> bool:
        try:
            kr = self._kr.get_keyring() if hasattr(self._kr, "get_keyring") else self._kr
            name = f"{type(kr).__module__}.{type(kr).__name__}"
        except Exception:  # noqa: BLE001
            return False
        # keyring's fail / null backends mean "no usable secure storage".
        return not any(bad in name for bad in ("backends.fail", "backends.null"))

    def _require(self) -> None:
        if not self.available():
            raise SecretStoreError("secure storage (macOS Keychain) is unavailable", 503)

    @staticmethod
    def _check(name: str, internal: bool = False) -> None:
        if not _NAME_RE.match(name or ""):
            raise SecretStoreError("invalid secret name")
        if name == _INDEX:
            raise SecretStoreError("reserved name")
        # OAuth credentials are written by the server's own login flow only; the
        # page can neither set nor overwrite them.
        if name.startswith("oauth:") and not internal:
            raise SecretStoreError("managed by the server", 403)
        if name.startswith(BLOCKED_PREFIXES):
            raise SecretStoreError("not supported in the web version", 403)

    # ---- index (names + timestamps only, never values) ----------------------

    def _read_index(self) -> dict[str, str]:
        raw = self._kr.get_password(SERVICE, _INDEX)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _write_index(self, index: dict[str, str]) -> None:
        self._kr.set_password(SERVICE, _INDEX, json.dumps(index, sort_keys=True))

    # ---- operations -----------------------------------------------------------

    def set(self, name: str, value: str, *, _internal: bool = False) -> dict[str, str]:
        self._require()
        self._check(name, internal=_internal)
        if not isinstance(value, str) or not value:
            raise SecretStoreError("empty value")
        with self._lock:
            self._kr.set_password(SERVICE, name, value)
            index = self._read_index()
            index[name] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write_index(index)
            return {"key": name, "updatedAt": index[name]}

    def get(self, name: str) -> Optional[str]:
        """The raw value. Server-side use ONLY — never return this from an API."""
        self._require()
        if not _NAME_RE.match(name or "") or name == _INDEX:
            return None
        # Field references into the OAuth credential blob (never returned to the page).
        m = _OAUTH_FIELD.match(name)
        if m:
            raw = self._kr.get_password(SERVICE, "oauth:openai")
            try:
                return (json.loads(raw) or {}).get(m.group(1)) if raw else None
            except ValueError:
                return None
        return self._kr.get_password(SERVICE, name)

    def delete(self, name: str, *, _internal: bool = False) -> bool:
        self._require()
        self._check(name, internal=_internal)
        with self._lock:
            existed = self._kr.get_password(SERVICE, name) is not None
            if existed:
                self._kr.delete_password(SERVICE, name)
            index = self._read_index()
            if index.pop(name, None) is not None:
                self._write_index(index)
            return existed

    def list(self) -> list[dict[str, str]]:
        self._require()
        index = self._read_index()
        return [{"key": k, "updatedAt": v} for k, v in sorted(index.items())]

    def has(self, name: str) -> bool:
        return self.get(name) is not None
