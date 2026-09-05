"""rt_health.py — Lightweight HTTP Health & Readiness Server for Phone-Pal Worker.

Provides /health and /ready HTTP endpoints for container orchestrators (Docker, K8s, Fly).
Tracks worker uptime, active call session counts, memory stats, and health status.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
from typing import Any, Callable

_START_TIME = time.time()
_ACTIVE_SESSIONS = 0
_SESSION_LOCK = threading.Lock()
_LAST_ERROR: dict[str, Any] | None = None
_SERVER_THREAD: threading.Thread | None = None

# Readiness registry: name -> zero-arg callable returning truthy when the dependency is usable.
# Evaluated live on every /ready so a recovered dependency flips back to 200 without a restart.
_READINESS_CHECKS: dict[str, Callable[[], Any]] = {}
# Per-check result cache. Most checks are free and run live (ttl 0). The "db" probe is
# rt_prefs._db(), which prints a REFUSING line every time it fails; an orchestrator polling
# /ready every second would turn one bad SUPABASE_URL into a log flood, so its verdict is
# held for DEFAULT_CHECK_TTLS seconds. A recovered database is therefore seen at most
# that many seconds late, which readiness can afford and the log cannot.
DEFAULT_CHECK_TTLS: dict[str, float] = {"db": 5.0}
_CHECK_TTLS: dict[str, float] = {}
_CHECK_CACHE: dict[str, tuple[float, bool]] = {}   # name -> (monotonic expiry, result)
_CHECKS_LOCK = threading.Lock()


def register_check(name: str, fn: Callable[[], Any], ttl: float | None = None) -> None:
    """Register (or replace) a named readiness check.

    `ttl` seconds caches the verdict; None picks DEFAULT_CHECK_TTLS (0 = live) for the name.
    Re-registering drops any cached verdict so a replacement check is consulted at once.
    """
    with _CHECKS_LOCK:
        _READINESS_CHECKS[name] = fn
        _CHECK_TTLS[name] = float(DEFAULT_CHECK_TTLS.get(name, 0.0) if ttl is None else ttl)
        _CHECK_CACHE.pop(name, None)


def unregister_check(name: str) -> None:
    """Remove a check and its cached verdict (tests and harnesses clean up with this)."""
    with _CHECKS_LOCK:
        _READINESS_CHECKS.pop(name, None)
        _CHECK_TTLS.pop(name, None)
        _CHECK_CACHE.pop(name, None)


def run_checks() -> dict[str, bool]:
    """Run every registered check now. A raising check counts as failed (fail-closed)."""
    now = time.monotonic()
    with _CHECKS_LOCK:
        checks = list(_READINESS_CHECKS.items())
        ttls = dict(_CHECK_TTLS)
        cache = dict(_CHECK_CACHE)
    results: dict[str, bool] = {}
    for name, fn in checks:
        ttl = ttls.get(name, 0.0)
        hit = cache.get(name)
        if ttl > 0 and hit is not None and hit[0] > now:
            results[name] = hit[1]
            continue
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        results[name] = ok
        if ttl > 0:
            with _CHECKS_LOCK:
                _CHECK_CACHE[name] = (now + ttl, ok)
    return results


def increment_active_sessions() -> int:
    global _ACTIVE_SESSIONS
    with _SESSION_LOCK:
        _ACTIVE_SESSIONS += 1
        return _ACTIVE_SESSIONS


def decrement_active_sessions() -> int:
    global _ACTIVE_SESSIONS
    with _SESSION_LOCK:
        _ACTIVE_SESSIONS = max(0, _ACTIVE_SESSIONS - 1)
        return _ACTIVE_SESSIONS


def get_active_sessions() -> int:
    with _SESSION_LOCK:
        return _ACTIVE_SESSIONS


def record_error(error_msg: str, details: dict[str, Any] | None = None) -> None:
    global _LAST_ERROR
    _LAST_ERROR = {
        "msg": error_msg,
        "ts": time.time(),
        "details": details or {},
    }


def _sanitize_error(err: dict[str, Any] | None) -> dict[str, Any] | None:
    """Sanitize error messages to prevent leaking internal database URLs or stack traces."""
    if not err:
        return None
    msg = str(err.get("msg") or "")
    # Keep only high-level summary, strip tracebacks / sensitive substrings
    clean_msg = msg.split("\n")[0][:120] if msg else "Internal error"
    return {
        "msg": clean_msg,
        "ts": err.get("ts"),
    }


SMS_WEBHOOK_PATHS = ("/sms/incoming", "/api/sms/incoming", "/sms")
USAGE_WEBHOOK_PATHS = ("/twilio/usage",)
MAX_WEBHOOK_BYTES = 64 * 1024   # a Twilio form is a few KB; anything bigger is not Twilio


def _webhook_url_candidates(handler: http.server.BaseHTTPRequestHandler) -> list[str]:
    """The URLs Twilio may have signed this request over.

    Twilio signs the URL typed into its console. Behind Caddy the worker sees
    127.0.0.1:8080, so RT_SMS_WEBHOOK_URL states the public one outright; the
    forwarded headers Caddy sets rebuild it when that is empty. The port is
    tried both ways because Twilio's own validator does.
    """
    out: list[str] = []
    configured = (os.getenv("RT_SMS_WEBHOOK_URL") or "").strip()
    if configured:
        out.append(configured)
    proto = (handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    host = (handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "").split(",")[0].strip()
    if host:
        out.append(f"{proto}://{host}{handler.path}")
        hostname, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
        if port:
            out.append(f"{proto}://{hostname}{handler.path}")
        elif proto == "https":
            out.append(f"https://{host}:443{handler.path}")
    return [u for i, u in enumerate(out) if u not in out[:i]]


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine health check HTTP access logs from polluting stdout
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")

        if path in ("/health", "/ready", "/live", ""):
            checks = run_checks()
            failed = sorted(name for name, ok in checks.items() if not ok)
            payload = {
                "status": "ok",
                "service": "phone-pal-worker",
                "uptime_seconds": round(time.time() - _START_TIME, 2),
                "active_sessions": get_active_sessions(),
                "last_error": _sanitize_error(_LAST_ERROR),
                "ready": not failed,
                "checks": checks,
            }
            status = 200
            # Only /ready gates traffic; /health and /live stay 200 so orchestrators don't
            # restart a live process whose dependency is merely degraded.
            if path == "/ready" and failed:
                status = 503
                payload["status"] = "degraded"
                payload["failed"] = failed
            self._send_json(status, payload)
        elif path.startswith("/media/"):
            filename = os.path.basename(path)
            media_dir = os.path.join(os.path.dirname(__file__), "media")
            fpath = os.path.join(media_dir, filename)
            if os.path.exists(fpath) and os.path.isfile(fpath):
                content_type = "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png"
                with open(fpath, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_plain(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self) -> None:
        """Read and discard the request body, bounded.

        Answering a POST without reading its body closes the socket while the
        client is still writing it, and the client raises a connection reset
        instead of seeing the status — so a 404 here would read to Twilio as
        an unreachable worker rather than a wrong URL.
        """
        raw = (self.headers.get("Content-Length") or "0").strip()
        remaining = int(raw) if raw.isdigit() else 0
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 8192))
            if not chunk:
                break
            remaining -= len(chunk)

    def do_POST(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path not in SMS_WEBHOOK_PATHS and path not in USAGE_WEBHOOK_PATHS:
            self._drain()
            self.send_response(404)
            self.end_headers()
            return

        # Caddy forwards /sms/* here from the public internet. Until 2026-09-02
        # this handler trusted the form body outright: any POST naming a
        # caller's number in `From` was answered with that caller's memories
        # and written into their ledger. Now nothing past this point runs
        # unless X-Twilio-Signature verifies under our auth token.
        import urllib.parse
        import rt_sms
        import rt_sms_inbound

        length_raw = (self.headers.get("Content-Length") or "0").strip()
        if not length_raw.isdigit() or int(length_raw) > MAX_WEBHOOK_BYTES:
            # Not drained: the point of the cap is not to read it.
            self._send_plain(413, "payload too large")
            return
        content_length = int(length_raw)
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        # keep_blank_values: Twilio signs `Body=` for a photo-only MMS, and
        # parse_qs drops blank fields, which would fail every such signature.
        params: dict[str, str | list[str]] = {}
        for k, v in urllib.parse.parse_qsl(raw_body, keep_blank_values=True):
            if k in params:
                cur = params[k]
                params[k] = [*cur, v] if isinstance(cur, list) else [cur, v]
            else:
                params[k] = v

        if not rt_sms.webhook_is_from_twilio(self.headers.get("X-Twilio-Signature"),
                                             _webhook_url_candidates(self), params):
            self._send_plain(403, "forbidden")
            return

        if path in USAGE_WEBHOOK_PATHS:
            # The daily spend alarm from twilio-setup.sh. Nothing in the body
            # is believed: rt_carrier re-reads today's spend from the account
            # and logs it. Signed like every other Twilio request, so a
            # stranger cannot even make the worker do that read.
            import rt_carrier
            rt_carrier.on_usage_trigger()
            self._send_plain(200, "ok")
            return

        def one(key: str) -> str:
            v = params.get(key, "")
            return v[0] if isinstance(v, list) else v

        num_media_raw = one("NumMedia").strip()
        num_media = int(num_media_raw) if num_media_raw.isdigit() else 0
        media_urls = [one(f"MediaUrl{i}") for i in range(min(num_media, 10))]
        rt_sms_inbound.accept_webhook(
            one("From"), one("To"), one("Body"),
            [m for m in media_urls if m],
            one("MessageSid") or one("SmsMessageSid"),
        )

        # An empty <Response/> acknowledges receipt; the reply goes out through
        # the REST API from a worker thread, so this answer never waits on the
        # model and Twilio never times out and retries.
        twiml = b'<?xml version="1.0" encoding="UTF-8"?>\n<Response/>'
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(twiml)))
        self.end_headers()
        self.wfile.write(twiml)


_SERVER_PORT: int | None = None
_SERVER_HOST: str | None = None


class _Server(http.server.ThreadingHTTPServer):
    """One thread per request. The plain HTTPServer this replaced served one
    request at a time, and once /sms/incoming moved onto it a single text
    (Supabase, two media fetches, a model call) held /ready for the duration,
    and an orchestrator polling readiness would have seen a dead worker mid-call."""
    allow_reuse_address = True
    daemon_threads = True


def _resolve_bind() -> tuple[str, int]:
    """Single source of truth for the bind address: RT_DOCKER_* wins, legacy HEALTH_* is the
    fallback, then loopback:8080. Loopback by default so host-networked containers don't
    expose an unauthenticated endpoint."""
    port_raw = os.getenv("RT_DOCKER_HEALTH_PORT") or os.getenv("HEALTH_PORT") or "8080"
    host = os.getenv("RT_DOCKER_HEALTH_HOST") or os.getenv("HEALTH_HOST") or "127.0.0.1"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8080
    return host, port


def get_health_port() -> int:
    return _SERVER_PORT or _resolve_bind()[1]


def get_health_host() -> str:
    return _SERVER_HOST or _resolve_bind()[0]


def start_health_server(port: int | None = None, host: str | None = None) -> threading.Thread | None:
    """Starts the health check HTTP server on a background daemon thread.

    Defaults to binding to 127.0.0.1 (localhost) to prevent unauthenticated
    exposure across public interfaces in host-networked containers.
    """
    global _SERVER_THREAD, _SERVER_PORT, _SERVER_HOST
    if _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
        return _SERVER_THREAD

    env_host, env_port = _resolve_bind()
    health_port = port or env_port
    health_host = host or env_host

    try:
        httpd = _Server((health_host, health_port), HealthHandler)
        _SERVER_PORT = health_port
        _SERVER_HOST = health_host
        _SERVER_THREAD = threading.Thread(
            target=httpd.serve_forever,
            name="HealthServerThread",
            daemon=True,
        )
        _SERVER_THREAD.start()
        print(f"[rt-health] Health check server listening on http://{health_host}:{health_port}/health", flush=True)
        return _SERVER_THREAD
    except Exception as exc:
        print(f"[rt-health] Warning: Failed to bind health check server on {health_host}:{health_port}: {exc}", file=sys.stderr, flush=True)
        return None


if __name__ == "__main__":
    start_health_server(8080)
    print("Health server running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[rt-health] Health server stopped.")
