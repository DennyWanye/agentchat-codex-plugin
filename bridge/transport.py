from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import AuthenticationError, ProtocolError, RemoteError


class JsonRpcTransport:
    def __init__(self, endpoint: str, *, timeout: float = 35.0, retries: int = 3, backoff: float = 0.5, context: ssl.SSLContext | None = None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.context = context or self._default_context()

    @staticmethod
    def _default_context() -> ssl.SSLContext:
        """Build a verifying context, including the macOS framework fallback.

        Some framework Python builds return an empty default trust store even
        though the system CA bundle is available at this path.  Loading it is
        additive and keeps normal hostname/certificate verification enabled.
        """
        context = ssl.create_default_context()
        ca_file = Path("/etc/ssl/cert.pem")
        # Framework Python may report a non-empty OpenSSL trust store that is
        # still missing certificates trusted by macOS. Loading the system
        # bundle is additive and does not disable hostname or chain checks.
        if ca_file.is_file():
            context.load_verify_locations(cafile=str(ca_file))
        return context

    def call(self, method: str, arguments: dict[str, Any] | None = None, *, bearer: str | None = None) -> Any:
        body = {"jsonrpc": "2.0", "id": random.randint(1, 2_000_000_000), "method": "tools/call", "params": {"name": method, "arguments": arguments or {}}}
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(self.endpoint, data=encoded, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                    status = int(response.status)
                    raw = response.read()
                return self._decode(raw, status)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code in {401, 403}:
                    raise AuthenticationError(self._error_text(raw), status=exc.code) from exc
                if exc.code == 429 or exc.code >= 500:
                    if attempt < self.retries:
                        time.sleep(self.backoff * (2**attempt))
                        continue
                raise RemoteError(self._error_text(raw), status=exc.code) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(self.backoff * (2**attempt))
                    continue
                raise RemoteError(f"network request failed: {exc}") from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _decode(raw: bytes, status: int) -> Any:
        try:
            outer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("server returned invalid JSON") from exc
        if not isinstance(outer, dict):
            raise ProtocolError("server returned a non-object JSON-RPC response")
        if outer.get("error"):
            error = outer["error"]
            message = error.get("message", "JSON-RPC error") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            if status in {401, 403}:
                raise AuthenticationError(message, status=status, code=str(code) if code else None)
            raise RemoteError(message, status=status, code=str(code) if code else None)
        result = outer.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("JSON-RPC response has no result")
        if result.get("isError"):
            content = result.get("content") or []
            message = content[0].get("text", "remote tool failed") if isinstance(content, list) and content and isinstance(content[0], dict) else "remote tool failed"
            lowered = str(message).lower()
            if any(term in lowered for term in ("unauthorized", "invalid access", "access token expired", "authentication required", "not authenticated")):
                raise AuthenticationError(str(message), status=status)
            raise RemoteError(str(message), status=status)
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        content = result.get("content") or []
        if not content:
            return {}
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if text is None:
            return content[0]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _error_text(raw: bytes) -> str:
        try:
            value = json.loads(raw.decode("utf-8"))
            if isinstance(value, dict):
                return str(value.get("message") or value.get("error") or value)
            return str(value)
        except Exception:
            return raw.decode("utf-8", errors="replace")[:500] or "remote request failed"
