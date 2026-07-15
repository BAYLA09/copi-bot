from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator

from abstractions import PositionChangeType, PositionEvent, PositionSource
from ctrader_api import CTraderAPIError, CTraderTradingClient
from ctrader_api_client.enums import ExecutionType
from models import Position

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = float(
    os.getenv("SOURCE_RECONCILE_INTERVAL", "10")
)


class CTraderOpenAPISource:
    """Real-time cTrader Copy position monitor via the official Open API."""

    def __init__(
        self,
        client: CTraderTradingClient,
        *,
        reconcile_interval: float = RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self.client = client
        self.reconcile_interval = reconcile_interval
        self._known: dict[str, Position] = {}
        self._event_queue: asyncio.Queue[PositionEvent] = asyncio.Queue()
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        await self.client.connect()
        self._known = await self.client.fetch_tracked_positions()
        logger.info(
            "Open API source connected. Initial open positions: %s",
            len(self._known),
        )
        for position in self._known.values():
            await self._event_queue.put(
                PositionEvent(PositionChangeType.OPEN, position)
            )

    async def disconnect(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.client.disconnect()

    async def stream_events(self) -> AsyncIterator[PositionEvent]:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._execution_loop(), name="execution-loop"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile-loop"),
        ]

        try:
            while self._running:
                event = await self._event_queue.get()
                yield event
        finally:
            await self.disconnect()

    async def _execution_loop(self) -> None:
        while self._running:
            try:
                execution = await self.client.wait_for_execution_event(timeout=1.0)
            except TimeoutError:
                continue
            except CTraderAPIError:
                logger.exception("Execution event loop error.")
                await asyncio.sleep(1)
                continue

            if execution.position_id is None:
                continue

            position_id = str(execution.position_id)
            if execution.execution_type in {
                ExecutionType.ORDER_FILLED,
                ExecutionType.ORDER_PARTIAL_FILL,
            }:
                await self._refresh_position(position_id, PositionChangeType.OPEN)
            elif execution.execution_type == ExecutionType.ORDER_REPLACED:
                await self._refresh_position(position_id, PositionChangeType.MODIFY)
            elif execution.execution_type in {
                ExecutionType.ORDER_CANCELLED,
                ExecutionType.ORDER_EXPIRED,
            }:
                await self._handle_possible_close(position_id)

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.reconcile_interval)
            try:
                await self._reconcile_positions()
            except Exception:
                logger.exception("Reconcile loop error.")

    async def _reconcile_positions(self) -> None:
        live = await self.client.fetch_tracked_positions()
        live_ids = set(live)

        for position_id, position in live.items():
            previous = self._known.get(position_id)
            if previous is None:
                self._known[position_id] = position
                await self._event_queue.put(
                    PositionEvent(PositionChangeType.OPEN, position)
                )
                logger.info("Reconcile detected OPEN: %s", position_id)
                continue

            if self._position_changed(previous, position):
                self._known[position_id] = position
                await self._event_queue.put(
                    PositionEvent(PositionChangeType.MODIFY, position, previous)
                )
                logger.info("Reconcile detected MODIFY: %s", position_id)

        for position_id in list(self._known):
            if position_id not in live_ids:
                previous = self._known.pop(position_id)
                closed = Position(
                    position_id=previous.position_id,
                    symbol=previous.symbol,
                    side=previous.side,
                    entry_price=previous.entry_price,
                    current_price=previous.current_price,
                    volume=previous.volume,
                    sl=previous.sl,
                    tp=previous.tp,
                    closed=True,
                )
                await self._event_queue.put(
                    PositionEvent(PositionChangeType.CLOSE, closed, previous)
                )
                logger.info("Reconcile detected CLOSE: %s", position_id)

    async def _refresh_position(
        self, position_id: str, change_type: PositionChangeType
    ) -> None:
        live = await self.client.fetch_tracked_positions()
        position = live.get(position_id)
        if position is None:
            await self._handle_possible_close(position_id)
            return

        previous = self._known.get(position_id)
        if change_type == PositionChangeType.OPEN and previous is not None:
            change_type = PositionChangeType.MODIFY

        self._known[position_id] = position
        await self._event_queue.put(
            PositionEvent(change_type, position, previous)
        )
        logger.info(
            "Execution event %s for position %s (%s)",
            change_type,
            position_id,
            position.symbol,
        )

    async def _handle_possible_close(self, position_id: str) -> None:
        live = await self.client.fetch_tracked_positions()
        if position_id in live:
            return

        previous = self._known.pop(position_id, None)
        if previous is None:
            return

        closed = Position(
            position_id=previous.position_id,
            symbol=previous.symbol,
            side=previous.side,
            entry_price=previous.entry_price,
            current_price=previous.current_price,
            volume=previous.volume,
            sl=previous.sl,
            tp=previous.tp,
            closed=True,
        )
        await self._event_queue.put(
            PositionEvent(PositionChangeType.CLOSE, closed, previous)
        )
        logger.info("Execution event CLOSE for position %s", position_id)

    @staticmethod
    def _position_changed(previous: Position, current: Position) -> bool:
        return (
            previous.volume != current.volume
            or previous.sl != current.sl
            or previous.tp != current.tp
            or previous.entry_price != current.entry_price
            or previous.side != current.side
            or previous.symbol != current.symbol
        )
