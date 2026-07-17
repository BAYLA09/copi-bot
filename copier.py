from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from ctrader_api import CTraderAPIError, CTraderTradingClient
from destinations.base import BaseDestinationExecutor
from destinations.ctrader import CTraderDestinationExecutor
from destinations.ftmo import FTMOTDestinationExecutor
from login import ensure_fresh_tokens
from models import MappingStore, Position

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
DESTINATION_BROKER = os.getenv("DESTINATION_BROKER", "ctrader").strip().lower()


def build_executor(mapping_store: MappingStore) -> BaseDestinationExecutor:
    if DESTINATION_BROKER == "ftmo":
        return FTMOTDestinationExecutor(mapping_store)

    client = CTraderTradingClient.from_env(role="destination")
    return CTraderDestinationExecutor(client, mapping_store)


def _executor_client(executor: BaseDestinationExecutor) -> CTraderTradingClient | None:
    if isinstance(executor, CTraderDestinationExecutor):
        return executor.client
    if isinstance(executor, FTMOTDestinationExecutor):
        return executor.client
    return None


class PositionCopier:
    """Watch positions.json and mirror trades through a destination executor."""

    def __init__(
        self,
        executor: BaseDestinationExecutor,
        positions_file: Path,
        mapping_store: MappingStore,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.executor = executor
        self.positions_file = positions_file
        self.mapping_store = mapping_store
        self.poll_interval = poll_interval
        self._positions_mtime: float | None = None
        self._pending_opens: set[str] = set()
        self._pending_closes: set[str] = set()
        self._pending_modifies: set[str] = set()
        self._last_snapshot: dict[str, Position] = {}

    async def run(self) -> None:
        logger.info(
            "Starting position copier via %s. Watching %s, mapping file %s",
            self.executor.name,
            self.positions_file,
            self.mapping_store.path,
        )
        while True:
            try:
                if self._positions_changed():
                    await self._process_positions()
            except CTraderAPIError:
                logger.exception("Destination API error while copying trades.")
                client = _executor_client(self.executor)
                if client is not None:
                    await client.reconnect()
            except NotImplementedError:
                logger.error("Destination broker is not implemented yet.")
                raise
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
                await self._handle_modified_position(source_id, position)

        await self._handle_missing_open_positions(positions)
        self._last_snapshot = positions

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
            destination_id = await self.executor.open_position(position)
            logger.info(
                "Mapped source position %s -> destination position %s",
                source_id,
                destination_id,
            )
        finally:
            self._pending_opens.discard(source_id)

    async def _handle_modified_position(self, source_id: str, position: Position) -> None:
        if not self.mapping_store.has_open_mapping(source_id):
            return
        if source_id in self._pending_modifies:
            return

        previous = self._last_snapshot.get(source_id)
        if previous is None or not self._position_modified(previous, position):
            return

        self._pending_modifies.add(source_id)
        try:
            logger.info(
                "Detected modify on source position %s: sl %s->%s tp %s->%s volume %s->%s",
                source_id,
                previous.sl,
                position.sl,
                previous.tp,
                position.tp,
                previous.volume,
                position.volume,
            )
            await self.executor.modify_position(source_id, position)
        finally:
            self._pending_modifies.discard(source_id)

    async def _handle_closed_position(self, source_id: str, position: Position) -> None:
        if source_id in self._pending_closes:
            return

        mapping = self.mapping_store.get(source_id)
        if mapping is None or mapping.status != "open":
            return

        self._pending_closes.add(source_id)
        try:
            logger.info(
                "Source position %s closed. Closing destination mapping %s.",
                source_id,
                mapping.destination_position_id,
            )
            await self.executor.close_position(source_id, position)
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

    @staticmethod
    def _position_modified(previous: Position, current: Position) -> bool:
        return (
            previous.sl != current.sl
            or previous.tp != current.tp
            or previous.volume != current.volume
        )


async def main() -> None:
    mapping_store = MappingStore(MAPPING_FILE)
    executor = build_executor(mapping_store)
    copier = PositionCopier(
        executor=executor,
        positions_file=POSITIONS_FILE,
        mapping_store=mapping_store,
    )

    ensure_fresh_tokens(role="destination")
    await executor.connect()
    try:
        await copier.run()
    except KeyboardInterrupt:
        logger.info("Copier stopped by user.")
    finally:
        await executor.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Copier stopped by user.")
        sys.exit(0)
