from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ctrader_api import CTraderAPIError, CTraderTradingClient
from models import MappingStore, Position, PositionMapping, PositionStore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = float(os.getenv("COPIER_POLL_INTERVAL", "1"))
POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))
MAPPING_FILE = Path(os.getenv("MAPPING_FILE", "mapping.json"))


class PositionCopier:
    """Watch positions.json and mirror trades on a destination cTrader account."""

    def __init__(
        self,
        api_client: CTraderTradingClient,
        positions_file: Path,
        mapping_store: MappingStore,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.api_client = api_client
        self.positions_file = positions_file
        self.mapping_store = mapping_store
        self.poll_interval = poll_interval
        self._positions_mtime: float | None = None
        self._pending_opens: set[str] = set()
        self._pending_closes: set[str] = set()

    async def run(self) -> None:
        logger.info(
            "Starting position copier. Watching %s, mapping file %s, poll interval %ss",
            self.positions_file,
            self.mapping_store.path,
            self.poll_interval,
        )
        while True:
            try:
                if self._positions_changed():
                    await self._process_positions()
            except CTraderAPIError:
                logger.exception("cTrader API error while copying trades.")
                await self._reconnect_api()
            except Exception:
                logger.exception("Unexpected copier error.")
            await asyncio.sleep(self.poll_interval)

    def _positions_changed(self) -> bool:
        if not self.positions_file.exists():
            return False

        mtime = self.positions_file.stat().st_mtime
        if self._positions_mtime is None or mtime != self._positions_mtime:
            self._positions_mtime = mtime
            return True
        return False

    def _load_positions(self) -> dict[str, Position]:
        if not self.positions_file.exists():
            return {}

        with self.positions_file.open(encoding="utf-8") as handle:
            raw = json.load(handle)

        return {
            position_id: Position.from_dict(item)
            for position_id, item in raw.items()
        }

    async def _process_positions(self) -> None:
        self.mapping_store.load()
        positions = self._load_positions()
        logger.debug("Loaded %s positions from %s", len(positions), self.positions_file)

        for source_id, position in positions.items():
            if position.closed:
                await self._handle_closed_position(source_id, position)
            else:
                await self._handle_open_position(source_id, position)

        await self._handle_missing_open_positions(positions)

    async def _handle_open_position(self, source_id: str, position: Position) -> None:
        if self.mapping_store.has_open_mapping(source_id):
            return
        if source_id in self._pending_opens:
            return

        self._pending_opens.add(source_id)
        try:
            logger.info(
                "Detected new source position %s: symbol=%s side=%s volume=%s",
                source_id,
                position.symbol,
                position.side,
                position.volume,
            )
            result = await self.api_client.open_position(
                symbol=position.symbol,
                side=position.side,
                volume_lots=position.volume,
                stop_loss=position.sl,
                take_profit=position.tp,
                label=source_id,
            )
            mapping = PositionMapping(
                source_position_id=source_id,
                destination_position_id=int(result["destination_position_id"]),
                symbol=position.symbol,
                side=position.side,
                volume=position.volume,
                api_volume=int(result["api_volume"]),
            )
            self.mapping_store.add(mapping)
            self.mapping_store.save()
            logger.info(
                "Mapped source position %s -> destination position %s",
                source_id,
                mapping.destination_position_id,
            )
        finally:
            self._pending_opens.discard(source_id)

    async def _handle_closed_position(self, source_id: str, position: Position) -> None:
        if source_id in self._pending_closes:
            return

        mapping = self.mapping_store.get(source_id)
        if mapping is None or mapping.status != "open":
            return

        self._pending_closes.add(source_id)
        try:
            logger.info(
                "Source position %s closed on monitor account. Closing destination %s.",
                source_id,
                mapping.destination_position_id,
            )
            await self.api_client.close_position(
                mapping.destination_position_id,
                mapping.api_volume,
            )
            self.mapping_store.mark_closed(source_id)
            self.mapping_store.save()
            logger.info(
                "Closed destination position %s for source position %s",
                mapping.destination_position_id,
                source_id,
            )
        finally:
            self._pending_closes.discard(source_id)

    async def _handle_missing_open_positions(
        self, positions: dict[str, Position]
    ) -> None:
        for mapping in self.mapping_store.iter_open_mappings():
            source_id = mapping.source_position_id
            current = positions.get(source_id)
            if current is None or current.closed:
                synthetic = current or Position(
                    position_id=source_id,
                    symbol=mapping.symbol,
                    side=mapping.side,
                    entry_price=0.0,
                    volume=mapping.volume,
                    closed=True,
                )
                await self._handle_closed_position(source_id, synthetic)

    async def _reconnect_api(self) -> None:
        try:
            await self.api_client.reconnect()
            logger.info("cTrader API reconnected successfully.")
        except Exception:
            logger.exception("Failed to reconnect to cTrader API.")
            await asyncio.sleep(self.poll_interval)


async def main() -> None:
    api_client = CTraderTradingClient.from_env()
    mapping_store = MappingStore(MAPPING_FILE)
    copier = PositionCopier(
        api_client=api_client,
        positions_file=POSITIONS_FILE,
        mapping_store=mapping_store,
    )

    await api_client.connect()
    try:
        await copier.run()
    except KeyboardInterrupt:
        logger.info("Copier stopped by user.")
    finally:
        await api_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Copier stopped by user.")
        sys.exit(0)
