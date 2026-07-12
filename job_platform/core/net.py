"""Polite async HTTP: retries, timeouts, per-host rate limiting, robots.txt.

All connector traffic goes through one `HttpClient` so no single source is
ever hammered regardless of how many companies are configured on it.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("net")


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids fetching a URL."""


class _HostGate:
    """Serializes requests to one host with a minimum interval between them."""

    def __init__(self, min_interval: float) -> None:
        self.lock = asyncio.Lock()
        self.min_interval = min_interval
        self.last_request = 0.0

    async def __aenter__(self) -> None:
        await self.lock.acquire()
        wait = self.min_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *exc: Any) -> None:
        self.last_request = time.monotonic()
        self.lock.release()


class HttpClient:
    def __init__(self, rate_cfg: dict[str, Any], respect_robots: bool = True) -> None:
        self.min_interval = float(rate_cfg.get("min_seconds_between_requests_per_host", 2.0))
        self.timeout = float(rate_cfg.get("request_timeout_seconds", 30))
        self.retries = int(rate_cfg.get("retries", 3))
        self.user_agent = rate_cfg.get("user_agent", "job-discovery-pipeline/1.0")
        self.respect_robots = respect_robots
        self._gates: dict[str, _HostGate] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._semaphore = asyncio.Semaphore(int(rate_cfg.get("max_concurrent_requests", 8)))
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _gate(self, host: str) -> _HostGate:
        if host not in self._gates:
            self._gates[host] = _HostGate(self.min_interval)
        return self._gates[host]

    async def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = urlsplit(url).netloc
        if host not in self._robots:
            robots_url = f"https://{host}/robots.txt"
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                resp = await self._client.get(robots_url)
                if resp.status_code == 200 and resp.text.strip():
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
            except httpx.HTTPError:
                parser = None  # unreachable robots.txt -> assume allowed
            self._robots[host] = parser
        parser = self._robots[host]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url) or parser.can_fetch("*", url)

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        check_robots: bool = True,
    ) -> httpx.Response:
        if check_robots and not await self._robots_allows(url):
            raise RobotsDisallowed(url)

        host = urlsplit(url).netloc

        @retry(
            stop=stop_after_attempt(self.retries),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        async def _do() -> httpx.Response:
            async with self._semaphore, self._gate(host):
                resp = await self._client.request(method, url, json=json_body, params=params)
            if resp.status_code in (429, 500, 502, 503, 504):
                resp.raise_for_status()  # retryable via tenacity
            return resp

        return await _do()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def post_json(self, url: str, json_body: Any) -> Any:
        resp = await self.request("POST", url, json_body=json_body)
        resp.raise_for_status()
        return resp.json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        resp = await self.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.text
