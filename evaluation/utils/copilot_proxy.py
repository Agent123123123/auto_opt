#!/usr/bin/env python3
"""
copilot_proxy.py
----------------
Lightweight HTTP proxy that exchanges a raw GitHub token for a Copilot session
token, then forwards requests to the real Copilot API with proper headers.

This solves the known opencode bug (anomalyco/opencode#19338) where non-GA
models (e.g. claude-sonnet-4.6) fail with "model not supported" because the
raw token is sent directly instead of a Copilot session token.

Usage:
    python copilot_proxy.py              # defaults: port=18976
    python copilot_proxy.py --port 9999

Configure opencode to use:
    "copilot-proxy": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Copilot Proxy",
        "options": { "baseURL": "http://127.0.0.1:18976/v1" },
        "models": { "claude-sonnet-4.6": {}, ... }
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("copilot_proxy")

# ---------------------------------------------------------------------------
# Session token cache
# ---------------------------------------------------------------------------

class CopilotTokenManager:
    """Exchanges a GitHub token for a Copilot session token, with caching."""

    EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
    REFRESH_MARGIN_S = 300  # refresh 5 min before expiry

    def __init__(self, github_token: str):
        self._github_token = github_token
        self._lock = threading.Lock()
        self._session_token: str | None = None
        self._api_endpoint: str = "https://api.individual.githubcopilot.com"
        self._expires_at: float = 0.0

    def get_token_and_endpoint(self) -> tuple[str, str]:
        with self._lock:
            if self._session_token and time.time() < self._expires_at - self.REFRESH_MARGIN_S:
                return self._session_token, self._api_endpoint
            return self._refresh()

    def _refresh(self) -> tuple[str, str]:
        log.info("Exchanging GitHub token for Copilot session token...")
        req = urllib.request.Request(
            self.EXCHANGE_URL,
            headers={
                "Authorization": "token " + self._github_token,
                "User-Agent": "opencode/1.3.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        self._session_token = data["token"]
        self._api_endpoint = data.get("endpoints", {}).get(
            "api", "https://api.individual.githubcopilot.com"
        )
        self._expires_at = float(data.get("expires_at", time.time() + 1800))
        log.info(
            "Got session token (expires in %ds), endpoint=%s",
            int(self._expires_at - time.time()),
            self._api_endpoint,
        )
        return self._session_token, self._api_endpoint


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

INTEGRATION_HEADERS = {
    "Editor-Version": "vscode/1.100.0",
    "Editor-Plugin-Version": "copilot-chat/1.0.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "opencode/1.3.0",
}


class ProxyHandler(BaseHTTPRequestHandler):
    token_mgr: CopilotTokenManager  # set by factory

    def do_POST(self):
        session_token, api_endpoint = self.token_mgr.get_token_and_endpoint()

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # Map path: /v1/chat/completions -> /chat/completions
        path = self.path
        if path.startswith("/v1"):
            path = path[3:]

        target_url = api_endpoint + path
        log.debug("Proxying POST %s -> %s (%d bytes)", self.path, target_url, len(body))

        headers = {
            "Authorization": "Bearer " + session_token,
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            **INTEGRATION_HEADERS,
        }

        upstream_req = urllib.request.Request(
            target_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(upstream_req, timeout=300) as upstream_resp:
                resp_body = upstream_resp.read()
                self.send_response(upstream_resp.status)
                for key in ("Content-Type",):
                    val = upstream_resp.headers.get(key)
                    if val:
                        self.send_header(key, val)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            log.warning("Upstream error %d: %s", e.code, err_body[:200])
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)

    def do_GET(self):
        if self.path in ("/health", "/v1/health"):
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        log.debug(format, *args)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Copilot session token proxy")
    parser.add_argument("--port", type=int, default=18976)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        log.error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    token_mgr = CopilotTokenManager(github_token)
    # Eagerly test exchange
    token_mgr.get_token_and_endpoint()

    ProxyHandler.token_mgr = token_mgr
    server = HTTPServer((args.host, args.port), ProxyHandler)
    log.info("Copilot proxy listening on http://%s:%d", args.host, args.port)
    print(f"COPILOT_PROXY_URL=http://{args.host}:{args.port}/v1", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
