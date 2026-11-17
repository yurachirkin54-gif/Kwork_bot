"""Shared async HTTP client: conditional requests, per-source proxy, sane headers."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

from app.core.rate_limiter import RateLimiter

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(slots=True)
class FetchResult:
    text: str
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    raw: bytes = b""


class HttpError(RuntimeError):
    """Non-2xx response surfaced to adapters for classification."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


@dataclass
class _CacheEntry:
    text: str
    etag: str | None = None
    last_modified: str | None = None
    payload: Any = None
    raw: bytes = b""


class HttpClient:
    """Thin httpx wrapper with ETag/If-Modified-Since caching and throttling."""

    def __init__(
        self,
        timeout: float = 20.0,
        proxy: str | None = None,
        limiter: RateLimiter | None = None,
        max_bytes: int = 4_000_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy or None
        self.limiter = limiter or RateLimiter(default_interval=0.8)
        self.max_bytes = max_bytes
        self._client = client
        self._owns_client = client is None
        self._cache: dict[str, _CacheEntry] = {}
        self._proxy_clients: dict[str | None, httpx.AsyncClient] = {}

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        for client in self._proxy_clients.values():
            await client.aclose()
        self._proxy_clients.clear()

    def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        """httpx >= 0.28 configures proxies per client, not per request."""

        if self._client is not None:
            return self._client
        key = proxy or None
        client = self._proxy_clients.get(key)
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
                proxy=key,
            )
            self._proxy_clients[key] = client
        return client

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            **BASE_HEADERS,
        }
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items() if v is not None})
        return headers

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        timeout: float | None = None,
        method: str = "GET",
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        conditional: bool = True,
        rate_key: str | None = None,
        min_interval: float | None = None,
    ) -> FetchResult:
        """Perform a request. Raises HttpError for non-2xx responses and httpx errors."""

        key = rate_key or httpx.URL(url).host or url
        await self.limiter.acquire(key, min_interval)
        request_headers = self._headers(headers)
        cached = self._cache.get(url) if conditional else None
        if cached is not None:
            if cached.etag:
                request_headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                request_headers["If-Modified-Since"] = cached.last_modified

        request_proxy = proxy or self.proxy
        try:
            response = await self._client_for(request_proxy).request(
                method.upper(),
                url,
                headers=request_headers,
                json=json_body,
                data=data,
                timeout=timeout or self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise HttpError(504, url, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise HttpError(502, url, str(exc)) from exc

        if response.status_code == 304 and cached is not None:
            return FetchResult(
                text=cached.text,
                status=304,
                url=str(response.url),
                headers=dict(response.headers),
                from_cache=True,
                raw=cached.raw,
            )

        raw = response.content
        body = self._decode(response, raw)
        if len(raw) > self.max_bytes:
            logger.debug(f"Ответ {url} обрезан до {self.max_bytes} байт")
            body = body[: self.max_bytes]
            raw = raw[: self.max_bytes]

        if response.status_code >= 400:
            raise HttpError(response.status_code, str(response.url), body[:4000])

        if conditional:
            self._cache[url] = _CacheEntry(
                text=body,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                raw=raw,
            )
        return FetchResult(
            text=body,
            status=response.status_code,
            url=str(response.url),
            headers=dict(response.headers),
            raw=raw,
        )

    @staticmethod
    def _decode(response: httpx.Response, raw: bytes) -> str:
        """Honour the declared charset; default to UTF-8 for JSON bodies.

        httpx falls back to charset detection when no charset is declared, which
        frequently mis-guesses JSON API responses as cp1251.
        """

        content_type = response.headers.get("content-type", "").lower()
        if "charset=" in content_type:
            return response.text
        stripped = raw.lstrip()[:1]
        if stripped in (b"{", b"["):
            return raw.decode("utf-8", errors="replace")
        return response.text

    def clear_cache(self) -> None:
        self._cache.clear()
