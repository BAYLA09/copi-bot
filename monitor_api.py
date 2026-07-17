from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from abstractions import PositionChangeType, PositionEvent
from env_file import upsert_env_values
from login import ensure_fresh_tokens
from ctrader_api import CTraderTradingClient
from models import PositionStore
from sources.ctrader_openapi import CTraderOpenAPISource

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "positions.json"))


def apply_event(store: PositionStore, event: PositionEvent) -> None:
    if event.change_type == PositionChangeType.CLOSE:
        store.mark_closed(event.position.position_id)
        return

    store.upsert(event.position)


async def run_monitor() -> None:
    ensure_fresh_tokens()
    store = PositionStore(POSITIONS_FILE)
    client = CTraderTradingClient.from_env(role="source")
    source = CTraderOpenAPISource(client)

    await source.connect()

    initial = await client.fetch_tracked_positions()
    for position in initial.values():
        store.upsert(position)
    store.save()
    logger.info(
        "Bootstrapped %s open positions into %s",
        len(initial),
        POSITIONS_FILE,
    )

    async for event in source.stream_events():
        apply_event(store, event)
        store.save()
        logger.info(
            "Position event: %s id=%s symbol=%s side=%s volume=%s sl=%s tp=%s",
            event.change_type,
            event.position.position_id,
            event.position.symbol,
            event.position.side,
            event.position.volume,
            event.position.sl,
            event.position.tp,
        )


def main() -> None:
    try:
        asyncio.run(run_monitor())
    except KeyboardInterrupt:
        logger.info("API monitor stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
