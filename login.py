#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from ctrader_api_client import CTraderClient, ClientConfig
from dotenv import load_dotenv

from env_file import upsert_env_values

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

OAUTH_AUTH_URL = (
    "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
)
OAUTH_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_SCOPE = "trading"
DEFAULT_API_HOST = "demo.ctraderapi.com"
ENV_FILE = Path(os.getenv("ENV_FILE", ".env"))
SOURCE_PREFIX = "CTRADER_SOURCE_"


class OAuthCallbackServer(HTTPServer):
    """Local HTTP server that receives the OAuth authorization redirect."""

    def __init__(self, server_address: tuple[str, int], redirect_path: str) -> None:
        super().__init__(server_address, OAuthCallbackHandler)
        self.redirect_path = redirect_path
        self.authorization_code: str | None = None
        self.error: str | None = None
        self.error_description: str | None = None
        self.state: str | None = None
        self.received = threading.Event()


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: OAuthCallbackServer

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("OAuth callback: " + format, *args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.redirect_path:
            self.send_error(404, "Not found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state_values = params.get("state", [])
        if self.server.state and state_values and state_values[0] != self.server.state:
            self.server.error = "invalid_state"
            self.server.error_description = "OAuth state mismatch."
            self._respond(400, "Invalid OAuth state. You can close this tab.")
            self.server.received.set()
            return

        if "error" in params:
            self.server.error = params["error"][0]
            self.server.error_description = params.get("error_description", [""])[0]
            self._respond(400, f"Authorization failed: {self.server.error}")
            self.server.received.set()
            return

        code_values = params.get("code")
        if not code_values:
            self.server.error = "missing_code"
            self.server.error_description = "No authorization code returned."
            self._respond(400, "Missing authorization code.")
            self.server.received.set()
            return

        self.server.authorization_code = code_values[0]
        self._respond(200, "Login successful. You can close this tab and return to the terminal.")
        self.server.received.set()

    def _respond(self, status: int, message: str) -> None:
        body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>cTrader OAuth</title></head>
<body style="font-family: sans-serif; padding: 2rem;">
  <h2>{message}</h2>
</body>
</html>"""
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _env(name: str) -> str:
    source_key = f"{SOURCE_PREFIX}{name}"
    return os.getenv(source_key, os.getenv(f"CTRADER_{name}", "")).strip()


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "product": "web",
            "state": state,
        }
    )
    return f"{OAUTH_AUTH_URL}?{query}"


def exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict[str, Any]:
    """Exchange an authorization code using the official REST endpoint."""
    query = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    url = f"{OAUTH_TOKEN_URL}?{query}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Token exchange failed ({exc.code}): {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Token exchange network error: {exc}") from exc

    if "accessToken" not in payload:
        raise RuntimeError(f"Unexpected token response: {payload}")

    return payload


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Refresh an access token using the official REST endpoint."""
    query = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    url = f"{OAUTH_TOKEN_URL}?{query}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if "accessToken" not in payload:
        raise RuntimeError(f"Unexpected refresh response: {payload}")

    return payload


def tokens_still_valid() -> bool:
    access_token = _env("ACCESS_TOKEN")
    expires_raw = _env("TOKEN_EXPIRES_AT")
    if not access_token or not expires_raw:
        return False

    try:
        expires_at = float(expires_raw)
    except ValueError:
        return False

    return time.time() < (expires_at - 300)


def save_tokens_to_env(
    *,
    client_id: str,
    client_secret: str,
    token_payload: dict[str, Any],
    trader_login: int,
    api_host: str,
    redirect_uri: str,
    scope: str,
) -> None:
    expires_in = int(token_payload.get("expiresIn", 0))
    expires_at = time.time() + expires_in

    upsert_env_values(
        ENV_FILE,
        {
            f"{SOURCE_PREFIX}CLIENT_ID": client_id,
            f"{SOURCE_PREFIX}CLIENT_SECRET": client_secret,
            f"{SOURCE_PREFIX}ACCESS_TOKEN": str(token_payload["accessToken"]),
            f"{SOURCE_PREFIX}REFRESH_TOKEN": str(token_payload["refreshToken"]),
            f"{SOURCE_PREFIX}TOKEN_EXPIRES_AT": str(int(expires_at)),
            f"{SOURCE_PREFIX}TRADER_LOGIN": str(trader_login),
            f"{SOURCE_PREFIX}API_HOST": api_host,
            f"{SOURCE_PREFIX}REDIRECT_URI": redirect_uri,
            f"{SOURCE_PREFIX}OAUTH_SCOPE": scope,
        },
    )


def preferred_trader_login() -> int | None:
    raw = _env("TRADER_LOGIN")
    if raw.isdigit():
        return int(raw)

    copy_url = os.getenv("COPY_URL", "")
    match = re.search(r"copyingAccount/(\d+)", copy_url)
    if match:
        return int(match.group(1))

    return None


async def resolve_trader_login(
    *,
    client_id: str,
    client_secret: str,
    access_token: str,
    api_host: str,
    preferred_login: int | None,
) -> int:
    """Resolve trader login using the official Open API account list."""
    config = ClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        host=api_host,
    )

    async with CTraderClient(config) as client:
        await client.auth.authenticate_app()
        accounts = await client.auth.get_accounts(access_token)

    if not accounts:
        raise RuntimeError(
            "No trading accounts were returned for this access token. "
            "Authorize at least one account during OAuth."
        )

    if preferred_login is not None:
        for account in accounts:
            if account.trader_login == preferred_login:
                logger.info(
                    "Using preferred trader login %s (account_id=%s)",
                    account.trader_login,
                    account.account_id,
                )
                return account.trader_login

    if len(accounts) == 1:
        account = accounts[0]
        logger.info(
            "Auto-selected trader login %s (account_id=%s)",
            account.trader_login,
            account.account_id,
        )
        return account.trader_login

    logger.info("Multiple accounts authorized:")
    for index, account in enumerate(accounts, start=1):
        logger.info(
            "  %s. trader_login=%s account_id=%s live=%s",
            index,
            account.trader_login,
            account.account_id,
            account.is_live,
        )

    while True:
        choice = input("Enter trader login to use: ").strip()
        if not choice.isdigit():
            print("Please enter a numeric trader login.")
            continue
        selected = int(choice)
        if any(account.trader_login == selected for account in accounts):
            return selected
        print("That trader login is not in the authorized account list.")


def wait_for_authorization_code(
    *,
    redirect_uri: str,
    state: str,
    timeout_seconds: int,
) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(
            f"Redirect URI must include host and port, got: {redirect_uri}"
        )

    redirect_path = parsed.path or "/"
    server = OAuthCallbackServer((parsed.hostname, parsed.port), redirect_path)
    server.state = state

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.info("Waiting for OAuth redirect on %s", redirect_uri)
    if not server.received.wait(timeout=timeout_seconds):
        server.shutdown()
        raise TimeoutError(
            f"Timed out after {timeout_seconds}s waiting for OAuth redirect."
        )

    server.shutdown()
    thread.join(timeout=2)

    if server.error:
        description = server.error_description or server.error
        raise RuntimeError(f"OAuth authorization failed: {description}")

    if not server.authorization_code:
        raise RuntimeError("OAuth callback received without an authorization code.")

    return server.authorization_code


def ensure_fresh_tokens(force_refresh: bool = False) -> None:
    """Refresh access tokens in .env when expired, using the official endpoint."""
    if tokens_still_valid() and not force_refresh:
        return

    client_id = _env("CLIENT_ID")
    client_secret = _env("CLIENT_SECRET")
    refresh_token = _env("REFRESH_TOKEN")
    trader_login = _env("TRADER_LOGIN")
    api_host = _env("API_HOST") or DEFAULT_API_HOST
    redirect_uri = _env("REDIRECT_URI") or DEFAULT_REDIRECT_URI
    scope = _env("OAUTH_SCOPE") or DEFAULT_SCOPE

    if not all([client_id, client_secret, refresh_token, trader_login]):
        raise SystemExit(
            "OAuth tokens are missing or expired. Run `python3 login.py` first."
        )

    logger.info("Access token expired or near expiry. Refreshing via official endpoint...")
    token_payload = refresh_access_token(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    )

    save_tokens_to_env(
        client_id=client_id,
        client_secret=client_secret,
        token_payload=token_payload,
        trader_login=int(trader_login),
        api_host=api_host,
        redirect_uri=redirect_uri,
        scope=scope,
    )
    load_dotenv(override=True)
    logger.info("Refreshed OAuth tokens saved to %s", ENV_FILE)


def run_login(
    *,
    force: bool = False,
    timeout_seconds: int = 300,
) -> None:
    if tokens_still_valid() and not force:
        logger.info(
            "Valid OAuth tokens already exist in %s. Skipping browser login.",
            ENV_FILE,
        )
        logger.info("Run `python3 monitor_api.py` or use --force to login again.")
        return

    client_id = _env("CLIENT_ID")
    client_secret = _env("CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Set CTRADER_SOURCE_CLIENT_ID and CTRADER_SOURCE_CLIENT_SECRET in .env "
            "before running login.py."
        )

    redirect_uri = _env("REDIRECT_URI") or DEFAULT_REDIRECT_URI
    scope = _env("OAUTH_SCOPE") or DEFAULT_SCOPE
    api_host = _env("API_HOST") or DEFAULT_API_HOST
    preferred_login = preferred_trader_login()

    state = secrets.token_urlsafe(24)
    auth_url = build_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state,
    )

    logger.info("Opening browser for cTrader OAuth authorization...")
    logger.info("If the browser does not open, visit this URL manually:\n%s", auth_url)
    webbrowser.open(auth_url, new=1)

    code = wait_for_authorization_code(
        redirect_uri=redirect_uri,
        state=state,
        timeout_seconds=timeout_seconds,
    )
    logger.info("Authorization code received. Exchanging for tokens...")

    token_payload = exchange_authorization_code(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=code,
    )

    trader_login = asyncio.run(
        resolve_trader_login(
            client_id=client_id,
            client_secret=client_secret,
            access_token=str(token_payload["accessToken"]),
            api_host=api_host,
            preferred_login=preferred_login,
        )
    )

    save_tokens_to_env(
        client_id=client_id,
        client_secret=client_secret,
        token_payload=token_payload,
        trader_login=trader_login,
        api_host=api_host,
        redirect_uri=redirect_uri,
        scope=scope,
    )

    logger.info("Saved OAuth tokens to %s", ENV_FILE)
    logger.info("Trader login: %s", trader_login)
    logger.info("You can now run: python3 monitor_api.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Official cTrader Open API OAuth login flow."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run browser login even if valid tokens already exist.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for OAuth redirect (default: 300).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        run_login(force=args.force, timeout_seconds=args.timeout)
    except KeyboardInterrupt:
        logger.info("Login cancelled.")
        sys.exit(1)
    except Exception as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
