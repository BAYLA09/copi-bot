from __future__ import annotations

import logging

from destinations.base import BaseDestinationExecutor
from models import Position

logger = logging.getLogger(__name__)


class FTMOTDestinationExecutor(BaseDestinationExecutor):
    """Placeholder for future FTMO execution integration."""

    name = "ftmo"

    async def connect(self) -> None:
        logger.info("FTMO executor is not implemented yet.")

    async def disconnect(self) -> None:
        return None

    async def open_position(self, position: Position) -> str:
        raise NotImplementedError(
            "FTMO execution is not implemented yet. "
            "This module is reserved for the next development step."
        )

    async def close_position(self, source_position_id: str, position: Position) -> None:
        raise NotImplementedError("FTMO execution is not implemented yet.")

    async def modify_position(
        self, source_position_id: str, position: Position
    ) -> None:
        raise NotImplementedError("FTMO execution is not implemented yet.")
