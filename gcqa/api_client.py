"""The only module that talks to the Safeguard API.

Behavior verified live against test order 500119014 (API_DOCUMENTATION.md):
- Basic auth -> Bearer JWT, expires_in 36000 s (10 h); refresh at exp - 300 s.
- Unknown order = HTTP 500 with body "failed workOrderClient.GetWorkOrderInfo"
  (there is no 404) -> OrderNotFound, never retried.
- Bad/expired token = plain-text 403 "Forbidden" -> refresh once, retry once.
- Image download: Bearer GET -> 302 -> pre-signed S3 URL (X-Amz-Expires=300).
  The redirect must be consumed immediately; pre-signed URLs are never cached.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import ApiCredentials

NOT_FOUND_MARKER = "failed workOrderClient.GetWorkOrderInfo"
TOKEN_REFRESH_MARGIN_S = 300
MAX_RETRIES = 3
PHOTO_CONCURRENCY = 8


class OrderNotFound(Exception):
    """The API's 500-with-marker 'not found' signal — an ops-queue item, never a review outcome."""


class AuthFailed(Exception):
    """401 from the auth endpoint — credential problem, nothing to retry."""


class ApiError(Exception):
    pass


@dataclass
class DownloadStats:
    listed: int = 0
    downloaded: int = 0
    failed: list[str] = field(default_factory=list)


class SafeguardClient:
    def __init__(self, creds: ApiCredentials, timeout_s: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._creds = creds
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(follow_redirects=True, timeout=timeout_s,
                                         transport=transport)
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self._photo_sem = asyncio.Semaphore(PHOTO_CONCURRENCY)

    async def __aenter__(self) -> "SafeguardClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_exp:
                return self._token
            resp = await self._client.get(
                self._creds.auth_url,
                auth=(self._creds.auth_user, self._creds.auth_password),
                timeout=10.0,
            )
            if resp.status_code == 401:
                raise AuthFailed("401 from auth endpoint — check SAFEGUARD_AUTH_PASSWORD")
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            expires_in = float(data.get("expires_in", 36000))
            self._token_exp = time.monotonic() + expires_in - TOKEN_REFRESH_MARGIN_S
            return self._token

    async def _authed_get(self, url: str) -> httpx.Response:
        """GET with Bearer auth: one 403-refresh-retry, then 3x backoff for transient errors."""
        refreshed = False
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            token = await self._get_token()
            try:
                resp = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                await asyncio.sleep(min(8.0, 2 ** attempt) + random.random())
                continue
            if resp.status_code == 403 and not refreshed:
                refreshed = True
                await self._get_token(force=True)
                continue
            if resp.status_code == 500 and NOT_FOUND_MARKER in resp.text:
                raise OrderNotFound(url)
            if resp.status_code >= 500:
                last_exc = ApiError(f"{resp.status_code} from {url}: {resp.text[:200]}")
                await asyncio.sleep(min(8.0, 2 ** attempt) + random.random())
                continue
            resp.raise_for_status()
            return resp
        raise ApiError(f"exhausted retries for {url}") from last_exc

    async def get_luggage(self, order_number: str) -> dict:
        resp = await self._authed_get(f"{self._creds.api_base}/aiapi/luggage/{order_number}")
        return resp.json()

    async def get_images(self, order_number: str) -> dict:
        resp = await self._authed_get(f"{self._creds.api_base}/aiapi/images/{order_number}")
        return resp.json()

    async def download_photo(self, web_file_name: str, dest: Path) -> Path:
        """Bearer GET -> 302 -> pre-signed S3 (followed automatically, consumed immediately)."""
        async with self._photo_sem:
            resp = await self._authed_get(f"{self._creds.image_base}/{web_file_name.lstrip('/')}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            return dest

    async def download_all(self, items: list[dict], cache_dir: Path) -> DownloadStats:
        """Download every item (dicts with webFileName + guid); one failure never kills the batch."""
        stats = DownloadStats(listed=len(items))

        async def one(item: dict) -> None:
            guid = item["guid"]
            dest = cache_dir / f"{guid}.jpg"
            if dest.exists():
                stats.downloaded += 1
                return
            try:
                await self.download_photo(item["webFileName"], dest)
                stats.downloaded += 1
            except Exception:
                stats.failed.append(guid)

        await asyncio.gather(*(one(i) for i in items))
        return stats
