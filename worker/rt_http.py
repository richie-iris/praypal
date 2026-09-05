"""rt_http.py — shared HTTP client for Phone-Pal (Iris): retries, timeouts, telemetry.

One urllib opener shared by Supabase REST/RPC, Resend and the web tools. There is NO
connection pool: urllib's HTTP handler forces "Connection: close" on the wire, so every
attempt opens a fresh socket. What this module actually owns is the bounded, classified
retry, the per-attempt timeout (optionally clamped to a total deadline) and the net.*
telemetry — not socket reuse.
"""
from __future__ import annotations

import rt_obs

import contextlib
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Module-level so tests can monkeypatch the backoff / clock without stalling the suite.
_SLEEP = time.sleep
_CLOCK = time.monotonic

# Bounded, classified retry. Only failures that plausibly clear on their own are
# retried: a reset socket, a timeout, or an upstream gateway hiccup. 4xx and the other 5xx are the server's verdict — retrying them just
# repeats the mistake (and a repeated mutating RPC is worse than a failed one).
MAX_ATTEMPTS = 3
_BACKOFF_S = (0.3, 0.9)
_RETRY_STATUSES = frozenset({502, 503, 504})


def _is_transient(e: BaseException) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return e.code in _RETRY_STATUSES
    # socket.timeout is TimeoutError on 3.10+; ConnectionError covers RemoteDisconnected
    # on a pooled socket the server closed between requests.
    return isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError))


class PooledHttpClient:
    """urllib-backed client: one opener, fresh socket per attempt, bounded retries."""

    def __init__(self, user_agent: str = "IrisPhonePal/1.0", timeout: float = 10.0):
        self.user_agent = user_agent
        self.default_timeout = timeout
        self._opener = urllib.request.build_opener()

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        data: dict | list | str | bytes | None = None,
        timeout: float | None = None,
        retries: int = 0,
        deadline_s: float | None = None,
    ) -> Any:
        """Execute an HTTP request with JSON payload support and bounded retries.

        Transient failures (see _is_transient) are retried up to min(retries+1,
        MAX_ATTEMPTS) total attempts with _BACKOFF_S between them; everything else
        raises on the spot. The DEFAULT is retries=0 — one send, ever — because a
        read-timeout after the bytes left cannot tell "never arrived" from "arrived
        and committed", and this client fronts Resend and every Supabase write.
        A caller that knows its call is idempotent (a read) opts in with retries=N.

        deadline_s is a total wall-clock budget for the whole call: each attempt's
        socket timeout is clamped to what is left of it, and a retry is skipped when
        elapsed + backoff would overrun it. None means per-attempt timeout only.
        Returns parsed JSON for a JSON body, None for an empty body, text otherwise.
        """
        req_headers = {
            "User-Agent": self.user_agent,
            # Advisory only: urllib rewrites this to "close" on the wire. Kept so the
            # intent survives if the transport is ever swapped for one that pools.
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)

        encoded_data = None
        if data is not None:
            if isinstance(data, (dict, list)):
                encoded_data = json.dumps(data).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            elif isinstance(data, str):
                encoded_data = data.encode("utf-8")
            elif isinstance(data, bytes):
                encoded_data = data

        # Only web schemes leave this client; file:/data: would read local state.
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"rt_http: unsupported URL scheme {scheme!r}")
        req = urllib.request.Request(  # noqa: S310 - scheme validated above
            url,
            data=encoded_data,
            headers=req_headers,
            method=method.upper(),
        )

        effective_timeout = timeout if timeout is not None else self.default_timeout

        obs_host = None
        obs_path = None
        obs_method = None
        with contextlib.suppress(Exception):
            parsed = urllib.parse.urlsplit(url)
            obs_host = parsed.hostname
            obs_path = parsed.path or "/"
            obs_method = method.upper()

        attempts = max(1, min(int(retries) + 1, MAX_ATTEMPTS))
        budget = float(deadline_s) if deadline_s is not None else None
        t_start = _CLOCK()
        last_exc = None
        for attempt in range(attempts):
            attempt_timeout = effective_timeout
            if budget is not None:
                remaining = budget - (_CLOCK() - t_start)
                if remaining <= 0:
                    # Budget already spent (a slow first attempt): do not open another
                    # socket just to time out on it.
                    raise last_exc or TimeoutError(f"deadline of {budget}s spent before {url}")
                attempt_timeout = min(effective_timeout, remaining)
            t0 = time.perf_counter()
            try:
                with self._opener.open(req, timeout=attempt_timeout) as resp:
                    resp_bytes = resp.read()
                    with contextlib.suppress(Exception):
                        rt_obs.obs.event(
                            "net.request",
                            host=obs_host,
                            path=obs_path,
                            method=obs_method,
                            ms=round((time.perf_counter() - t0) * 1000, 1),
                            status=getattr(resp, "status", None),
                            bytes=len(resp_bytes),
                            attempt=attempt,
                        )
                    if not resp_bytes:
                        return None
                    text = resp_bytes.decode("utf-8")
                    content_type = ""
                    with contextlib.suppress(Exception):
                        content_type = resp.headers.get("Content-Type", "") or ""
                    if "application/json" in content_type:
                        return json.loads(text)
                    # Supabase/PostgREST occasionally omits the content type on RPC
                    # replies; a body that parses as JSON is JSON.
                    try:
                        return json.loads(text)
                    except ValueError:
                        return text
            except Exception as e:
                rt_obs.obs.caught("rt_http.request", e)
                with contextlib.suppress(Exception):
                    err_ms = round((time.perf_counter() - t0) * 1000, 1)
                    err_status = getattr(e, "code", None)
                    if err_status is not None:
                        rt_obs.obs.event(
                            "net.request",
                            host=obs_host,
                            path=obs_path,
                            method=obs_method,
                            ms=err_ms,
                            status=err_status,
                            attempt=attempt,
                        )
                    rt_obs.obs.event(
                        "net.failed",
                        host=obs_host,
                        path=obs_path,
                        method=obs_method,
                        ms=err_ms,
                        status=err_status,
                        err=type(e).__name__,
                        attempt=attempt,
                    )
                last_exc = e
                if not _is_transient(e) or attempt >= attempts - 1:
                    raise
                backoff = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                if budget is not None and (_CLOCK() - t_start) + backoff >= budget:
                    # Sleeping would eat the rest of the budget; the caller gets the
                    # last failure now instead of a retry that cannot finish in time.
                    with contextlib.suppress(Exception):
                        rt_obs.obs.event("net.failed", host=obs_host, path=obs_path,
                                         method=obs_method, ms=round(
                                             (_CLOCK() - t_start) * 1000, 1),
                                         err="deadline_spent", attempt=attempt + 1)
                    raise
                with contextlib.suppress(Exception):
                    rt_obs.obs.event("net.retry", host=obs_host, path=obs_path,
                                     attempt=attempt + 1, err=type(e).__name__)
                _SLEEP(backoff)

        raise last_exc or RuntimeError(f"HTTP request to {url} failed")


http_client = PooledHttpClient()


if __name__ == "__main__":
    print("Testing rt_http PooledHttpClient...")
    res = http_client.request("GET", "https://httpbin.org/get")
    print("HTTP request successful:", isinstance(res, dict))
