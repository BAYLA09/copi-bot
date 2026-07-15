from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from models import Position


class PositionChangeType(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    MODIFY = "modify"


@dataclass(frozen=True, slots=True)
class PositionEvent:
    """A normalized position lifecycle event from any source."""

    change_type: PositionChangeType
    position: Position
    previous: Position | None = None


PositionEventHandler = Callable[[PositionEvent], Awaitable[None]]


class PositionSource(Protocol):
    """Monitors a source account and emits position lifecycle events."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def stream_events(self) -> AsyncIterator[PositionEvent]: ...


class DestinationExecutor(Protocol):
    """Executes mirrored trades on a destination broker."""

    name: str

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def open_position(self, position: Position) -> str:
        """Open a trade. Returns a destination-side identifier."""
        ...

    async def close_position(self, source_position_id: str, position: Position) -> None: ...

    async def modify_position(
        self, source_position_id: str, position: Position
    ) -> None: ...
