from __future__ import annotations

import logging
import os
from dataclasses import replace

from ctrader_api import CTraderAPIError, CTraderTradingClient
from destinations.base import BaseDestinationExecutor
from destinations.ctrader import CTraderDestinationExecutor
from models import MappingStore, Position

logger = logging.getLogger(__name__)

FTMO_DEFAULT_API_HOST = "live.ctraderapi.com"


class FTMOTDestinationExecutor(BaseDestinationExecutor):
    """Execute mirrored trades on an FTMO cTrader account via the live Open API."""

    name = "ftmo"

    def __init__(
        self,
        mapping_store: MappingStore,
        *,
        volume_multiplier: float | None = None,
    ) -> None:
        multiplier_raw = volume_multiplier
        if multiplier_raw is None:
            multiplier_raw = float(os.getenv("FTMO_VOLUME_MULTIPLIER", "1"))
        if multiplier_raw <= 0:
            raise CTraderAPIError("FTMO_VOLUME_MULTIPLIER must be greater than zero.")

        self.volume_multiplier = multiplier_raw
        self.mapping_store = mapping_store
        self.client = CTraderTradingClient.from_env(role="destination")
        self._ctrader = CTraderDestinationExecutor(self.client, mapping_store)

    async def connect(self) -> None:
        if self.client.host != FTMO_DEFAULT_API_HOST:
            logger.warning(
                "FTMO executor expected API host %s but got %s. "
                "Set CTRADER_API_HOST=%s in .env.",
                FTMO_DEFAULT_API_HOST,
                self.client.host,
                FTMO_DEFAULT_API_HOST,
            )

        logger.info(
            "Connecting FTMO destination executor: trader_login=%s host=%s volume_multiplier=%s",
            self.client.trader_login,
            self.client.host,
            self.volume_multiplier,
        )
        await self._ctrader.connect()

    async def disconnect(self) -> None:
        await self._ctrader.disconnect()

    async def reconnect(self) -> None:
        await self.client.reconnect()

    def _scaled_position(self, position: Position) -> Position:
        if self.volume_multiplier == 1.0:
            return position

        scaled_volume = round(position.volume * self.volume_multiplier, 2)
        if scaled_volume <= 0:
            raise CTraderAPIError(
                f"Scaled FTMO volume is invalid for source position {position.position_id}: "
                f"{scaled_volume}"
            )

        logger.info(
            "Scaling source volume %.2f -> %.2f lots for FTMO (multiplier=%s)",
            position.volume,
            scaled_volume,
            self.volume_multiplier,
        )
        return replace(position, volume=scaled_volume)

    async def open_position(self, position: Position) -> str:
        return await self._ctrader.open_position(self._scaled_position(position))

    async def close_position(self, source_position_id: str, position: Position) -> None:
        await self._ctrader.close_position(
            source_position_id,
            self._scaled_position(position),
        )

    async def modify_position(
        self, source_position_id: str, position: Position
    ) -> None:
        await self._ctrader.modify_position(
            source_position_id,
            self._scaled_position(position),
        )
