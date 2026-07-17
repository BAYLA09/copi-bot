from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any

from ctrader_api_client import CTraderClient, ClientConfig
from ctrader_api_client.enums import ExecutionType, OrderSide, OrderType
from ctrader_api_client.events import (
    ClientDisconnectEvent,
    ExecutionEvent,
    ReadyEvent,
    ReconnectedEvent,
    TokenInvalidatedEvent,
)
from ctrader_api_client.models import ClosePositionRequest, NewOrderRequest, Symbol
from dotenv import load_dotenv

from models import Position as TrackedPosition

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_API_HOST = "demo.ctraderapi.com"
SOURCE_ENV_PREFIX = "CTRADER_SOURCE_"
DEST_ENV_PREFIX = "CTRADER_"


class CTraderAPIError(RuntimeError):
    """Raised when a cTrader Open API operation fails."""


def _env(name: str, *, prefix: str = DEST_ENV_PREFIX, fallback: str = "") -> str:
    prefixed = os.getenv(f"{prefix}{name}", "").strip()
    if prefixed:
        return prefixed
    if prefix != DEST_ENV_PREFIX:
        return os.getenv(f"{DEST_ENV_PREFIX}{name}", fallback).strip()
    return fallback


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
        self._symbol_names: dict[int, str] = {}
        self._execution_queue: asyncio.Queue[ExecutionEvent] = asyncio.Queue()
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
    def from_env(cls, *, role: str = "destination") -> CTraderTradingClient:
        prefix = SOURCE_ENV_PREFIX if role == "source" else DEST_ENV_PREFIX
        client_id = _env("CLIENT_ID", prefix=prefix)
        client_secret = _env("CLIENT_SECRET", prefix=prefix)
        access_token = _env("ACCESS_TOKEN", prefix=prefix)
        refresh_token = _env("REFRESH_TOKEN", prefix=prefix)
        expires_raw = _env("TOKEN_EXPIRES_AT", prefix=prefix)
        trader_login_raw = _env("TRADER_LOGIN", prefix=prefix)
        host = _env("API_HOST", prefix=prefix, fallback="")
        if not host:
            host = "live.ctraderapi.com" if prefix == DEST_ENV_PREFIX else DEFAULT_API_HOST

        missing = [
            name
            for name, value in {
                f"{prefix}CLIENT_ID": client_id,
                f"{prefix}CLIENT_SECRET": client_secret,
                f"{prefix}ACCESS_TOKEN": access_token,
                f"{prefix}REFRESH_TOKEN": refresh_token,
                f"{prefix}TOKEN_EXPIRES_AT": expires_raw,
                f"{prefix}TRADER_LOGIN": trader_login_raw,
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
        @self._client.on(ExecutionEvent)
        async def on_execution(event: ExecutionEvent) -> None:
            if self._account_id is not None and event.account_id != self._account_id:
                return
            await self._execution_queue.put(event)

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

    async def get_symbol_name(self, symbol_id: int) -> str:
        if symbol_id in self._symbol_names:
            return self._symbol_names[symbol_id]

        symbols = await self._client.symbols.list_all(self.account_id)
        for item in symbols:
            self._symbol_names[item.symbol_id] = item.name.upper()

        return self._symbol_names.get(symbol_id, f"SYMBOL_{symbol_id}")

    async def fetch_tracked_positions(self) -> dict[str, TrackedPosition]:
        api_positions = await self._client.trading.get_open_positions(self.account_id)
        tracked: dict[str, TrackedPosition] = {}

        for api_position in api_positions:
            symbol_name = await self.get_symbol_name(api_position.symbol_id)
            symbol_info = await self.resolve_symbol(symbol_name)
            volume_lots = float(
                symbol_info.volume_to_lots(api_position.volume)
            )
            side = "Buy" if api_position.side == OrderSide.BUY else "Sell"
            position_id = str(api_position.position_id)

            tracked[position_id] = TrackedPosition(
                position_id=position_id,
                symbol=symbol_name,
                side=side,
                entry_price=float(api_position.entry_price),
                current_price=None,
                volume=volume_lots,
                sl=(
                    float(api_position.stop_loss)
                    if api_position.stop_loss is not None
                    else None
                ),
                tp=(
                    float(api_position.take_profit)
                    if api_position.take_profit is not None
                    else None
                ),
            )

        return tracked

    async def wait_for_execution_event(self, timeout: float | None = 30.0) -> ExecutionEvent:
        if timeout is None:
            return await self._execution_queue.get()
        return await asyncio.wait_for(self._execution_queue.get(), timeout=timeout)

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
