from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

from ctrader_api_client import CTraderClient, ClientConfig
from ctrader_api_client.enums import OrderSide, OrderType
from ctrader_api_client.events import (
    ClientDisconnectEvent,
    ReadyEvent,
    ReconnectedEvent,
    TokenInvalidatedEvent,
)
from ctrader_api_client.models import ClosePositionRequest, NewOrderRequest, Symbol
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_API_HOST = "demo.ctraderapi.com"


class CTraderAPIError(RuntimeError):
    """Raised when a cTrader Open API operation fails."""


class CTraderTradingClient:
    """Async wrapper around cTrader Open API with automatic reconnection."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        token_expires_at: float,
        trader_login: int,
        host: str = DEFAULT_API_HOST,
        port: int = 5035,
    ) -> None:
        self.trader_login = trader_login
        self.host = host
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._account_id: int | None = None
        self._symbol_cache: dict[str, Symbol] = {}
        self._client = CTraderClient(
            ClientConfig(
                client_id=client_id,
                client_secret=client_secret,
                host=host,
                port=port,
            )
        )
        self._register_event_handlers()

    @classmethod
    def from_env(cls) -> CTraderTradingClient:
        client_id = os.getenv("CTRADER_CLIENT_ID", "").strip()
        client_secret = os.getenv("CTRADER_CLIENT_SECRET", "").strip()
        access_token = os.getenv("CTRADER_ACCESS_TOKEN", "").strip()
        refresh_token = os.getenv("CTRADER_REFRESH_TOKEN", "").strip()
        expires_raw = os.getenv("CTRADER_TOKEN_EXPIRES_AT", "").strip()
        trader_login_raw = os.getenv("CTRADER_TRADER_LOGIN", "").strip()
        host = os.getenv("CTRADER_API_HOST", DEFAULT_API_HOST).strip()

        missing = [
            name
            for name, value in {
                "CTRADER_CLIENT_ID": client_id,
                "CTRADER_CLIENT_SECRET": client_secret,
                "CTRADER_ACCESS_TOKEN": access_token,
                "CTRADER_REFRESH_TOKEN": refresh_token,
                "CTRADER_TOKEN_EXPIRES_AT": expires_raw,
                "CTRADER_TRADER_LOGIN": trader_login_raw,
            }.items()
            if not value
        ]
        if missing:
            raise CTraderAPIError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        try:
            token_expires_at = float(expires_raw)
            trader_login = int(trader_login_raw)
        except ValueError as exc:
            raise CTraderAPIError(
                "CTRADER_TOKEN_EXPIRES_AT and CTRADER_TRADER_LOGIN must be numeric."
            ) from exc

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            trader_login=trader_login,
            host=host,
        )

    @property
    def account_id(self) -> int:
        if self._account_id is None:
            raise CTraderAPIError("cTrader account is not authenticated yet.")
        return self._account_id

    def _register_event_handlers(self) -> None:
        @self._client.on(ReconnectedEvent)
        async def on_reconnected(event: ReconnectedEvent) -> None:
            logger.warning(
                "cTrader API reconnected. Restored accounts=%s failed_accounts=%s",
                event.restored_accounts,
                event.failed_accounts,
            )

        @self._client.on(ReadyEvent)
        async def on_ready(event: ReadyEvent) -> None:
            logger.info("cTrader account ready: account_id=%s", event.account_id)
            self._account_id = event.account_id

        @self._client.on(ClientDisconnectEvent)
        async def on_disconnect(event: ClientDisconnectEvent) -> None:
            logger.error("cTrader API disconnected: reason=%s", event.reason)

        @self._client.on(TokenInvalidatedEvent)
        async def on_token_invalidated(event: TokenInvalidatedEvent) -> None:
            logger.error(
                "cTrader access token invalidated for accounts=%s",
                event.account_ids,
            )

    async def connect(self) -> None:
        logger.info("Connecting to cTrader Open API host=%s", self.host)
        await self._client.__aenter__()
        await self._client.auth.authenticate_app()
        logger.info("cTrader application authenticated.")

        credentials = await self._client.auth.authenticate_by_trader_login(
            trader_login=self.trader_login,
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            expires_at=self._token_expires_at,
        )
        self._account_id = credentials.account_id
        logger.info(
            "cTrader account authenticated: account_id=%s trader_login=%s",
            credentials.account_id,
            self.trader_login,
        )

    async def disconnect(self) -> None:
        logger.info("Disconnecting from cTrader Open API.")
        await self._client.__aexit__(None, None, None)

    async def reconnect(self) -> None:
        logger.warning("Manual cTrader API reconnect requested.")
        await self.disconnect()
        await self.connect()

    async def resolve_symbol(self, symbol_name: str) -> Symbol:
        normalized = symbol_name.upper().strip()
        cached = self._symbol_cache.get(normalized)
        if cached is not None:
            return cached

        matches = await self._client.symbols.search(self.account_id, normalized)
        if not matches:
            raise CTraderAPIError(f"Symbol not found on destination account: {normalized}")

        exact = next(
            (symbol for symbol in matches if symbol.name.upper() == normalized),
            matches[0],
        )
        symbol = await self._client.symbols.get_by_id(self.account_id, exact.symbol_id)
        self._symbol_cache[normalized] = symbol
        logger.info(
            "Resolved symbol %s -> symbol_id=%s lot_size=%s",
            normalized,
            symbol.symbol_id,
            symbol.lot_size,
        )
        return symbol

    def lots_to_api_volume(self, symbol: Symbol, volume_lots: float) -> int:
        api_volume = symbol.lots_to_volume(Decimal(str(volume_lots)))
        if api_volume <= 0:
            raise CTraderAPIError(
                f"Invalid API volume {api_volume} for {volume_lots} lots."
            )
        return api_volume

    async def open_position(
        self,
        *,
        symbol: str,
        side: str,
        volume_lots: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        symbol_info = await self.resolve_symbol(symbol)
        api_volume = self.lots_to_api_volume(symbol_info, volume_lots)
        order_side = OrderSide.BUY if side.lower().startswith("b") else OrderSide.SELL

        request = NewOrderRequest(
            symbol_id=symbol_info.symbol_id,
            side=order_side,
            volume=api_volume,
            order_type=OrderType.MARKET,
            stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
            take_profit=Decimal(str(take_profit)) if take_profit is not None else None,
            label=label,
            comment=f"copied-from-{label}" if label else "copied-position",
        )

        logger.info(
            "Opening destination position: symbol=%s side=%s volume_lots=%s api_volume=%s sl=%s tp=%s",
            symbol,
            side,
            volume_lots,
            api_volume,
            stop_loss,
            take_profit,
        )

        execution = await self._client.trading.place_order(self.account_id, request)
        if execution.position_id is None:
            raise CTraderAPIError(
                f"Open position failed for {symbol}: execution returned no position_id."
            )

        if execution.error_code:
            raise CTraderAPIError(
                f"Open position failed for {symbol}: error_code={execution.error_code}"
            )

        logger.info(
            "Destination position opened: position_id=%s order_id=%s fill_price=%s",
            execution.position_id,
            execution.order_id,
            execution.fill_price,
        )
        return {
            "destination_position_id": execution.position_id,
            "api_volume": api_volume,
            "order_id": execution.order_id,
            "fill_price": (
                float(execution.fill_price) if execution.fill_price is not None else None
            ),
        }

    async def close_position(self, destination_position_id: int, api_volume: int) -> None:
        request = ClosePositionRequest(
            position_id=destination_position_id,
            volume=api_volume,
        )
        logger.info(
            "Closing destination position: position_id=%s api_volume=%s",
            destination_position_id,
            api_volume,
        )

        execution = await self._client.trading.close_position(self.account_id, request)
        if execution.error_code:
            raise CTraderAPIError(
                "Close position failed for "
                f"{destination_position_id}: error_code={execution.error_code}"
            )

        logger.info(
            "Destination position closed: position_id=%s order_id=%s",
            destination_position_id,
            execution.order_id,
        )
