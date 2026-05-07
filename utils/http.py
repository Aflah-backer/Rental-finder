"""Shared async HTTP client with UA rotation, jitter, and retries."""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)

USER_AGENTS: list[str] = [
    # A small modern desktop pool. Real distributions rotate; we just need to look human.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
]

DEFAULT_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "20"))
DEFAULT_HEADERS_BASE: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _proxy() -> str | None:
    return os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or None


def make_client(
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
    http2: bool = True,
) -> httpx.AsyncClient:
    """Create a configured async client. Caller owns its lifetime."""
    base = dict(DEFAULT_HEADERS_BASE)
    base["User-Agent"] = random.choice(USER_AGENTS)
    if headers:
        base.update(headers)

    proxy = _proxy()
    return httpx.AsyncClient(
        headers=base,
        timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        follow_redirects=follow_redirects,
        http2=http2,
        proxy=proxy,
    )


async def polite_sleep(min_s: float = 0.5, max_s: float = 2.0) -> None:
    """Random jitter between requests to look human."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 3,
    accept_statuses: tuple[int, ...] = (200,),
) -> httpx.Response:
    """GET with retry on transient failures and a non-acceptable status."""
    async for attempt in AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.HTTPStatusError, httpx.RemoteProtocolError)
        ),
    ):
        with attempt:
            req_headers = {"User-Agent": random.choice(USER_AGENTS)}
            if headers:
                req_headers.update(headers)
            resp = await client.get(url, params=params, headers=req_headers)
            if resp.status_code not in accept_statuses:
                log.debug("non-OK %s for %s", resp.status_code, url)
                resp.raise_for_status()
            return resp
    raise RuntimeError("unreachable")  # pragma: no cover


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_attempts: int = 3,
) -> httpx.Response:
    async for attempt in AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.HTTPStatusError, httpx.RemoteProtocolError)
        ),
    ):
        with attempt:
            req_headers = {"User-Agent": random.choice(USER_AGENTS)}
            if headers:
                req_headers.update(headers)
            resp = await client.post(url, json=json, headers=req_headers)
            resp.raise_for_status()
            return resp
    raise RuntimeError("unreachable")  # pragma: no cover
