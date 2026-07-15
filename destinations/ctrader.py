from __future__ import annotations

import logging

from ctrader_api import CTraderTradingClient
from destinations.base import BaseDestinationExecutor
from models import MappingStore, Position, PositionMapping

logger = logging.getLogger(__name__)


class CTraderDestinationExecutor(BaseDestinationExecutor):
    """Mirror trades on another cTrader account via Open API."""

    name = "ctrader"

    def __init__(
        self,
        client: CTraderTradingClient,
        mapping_store: MappingStore,
    ) -> None:
        self.client = client
        self.mapping_store = mapping_store

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def open_position(self, position: Position) -> str:
        if self.mapping_store.has_open_mapping(position.position_id):
            existing = self.mapping_store.get(position.position_id)
            if existing is not None:
                return str(existing.destination_position_id)

        result = await self.client.open_position(
            symbol=position.symbol,
            side=position.side,
            volume_lots=position.volume,
            stop_loss=position.sl,
            take_profit=position.tp,
            label=position.position_id,
        )
        destination_id = str(result["destination_position_id"])
        self.mapping_store.add(
            PositionMapping(
                source_position_id=position.position_id,
                destination_position_id=int(destination_id),
                symbol=position.symbol,
                side=position.side,
                volume=position.volume,
                api_volume=int(result["api_volume"]),
            )
        )
        self.mapping_store.save()
        logger.info(
            "cTrader destination opened %s for source %s",
            destination_id,
            position.position_id,
        )
        return destination_id

    async def close_position(self, source_position_id: str, position: Position) -> None:
        mapping = self.mapping_store.get(source_position_id)
        if mapping is None or mapping.status != "open":
            return

        await self.client.close_position(
            mapping.destination_position_id,
            mapping.api_volume,
        )
        self.mapping_store.mark_closed(source_position_id)
        self.mapping_store.save()
        logger.info(
            "cTrader destination closed %s for source %s",
            mapping.destination_position_id,
            source_position_id,
        )

    async def modify_position(
        self, source_position_id: str, position: Position
    ) -> None:
        mapping = self.mapping_store.get(source_position_id)
        if mapping is None or mapping.status != "open":
            return

        logger.info(
            "cTrader destination modify not implemented yet for source %s "
            "(SL=%s TP=%s volume=%s). Mapping preserved.",
            source_position_id,
            position.sl,
            position.tp,
            position.volume,
        )
