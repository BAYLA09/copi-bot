from __future__ import annotations

from abc import ABC, abstractmethod

from models import Position


class BaseDestinationExecutor(ABC):
    """Base class for destination broker execution backends."""

    name: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def open_position(self, position: Position) -> str: ...

    @abstractmethod
    async def close_position(self, source_position_id: str, position: Position) -> None:
        ...

    @abstractmethod
    async def modify_position(
        self, source_position_id: str, position: Position
    ) -> None: ...
