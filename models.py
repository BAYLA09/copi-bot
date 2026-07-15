from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Position:
    """A monitored position from the cTrader Copy investor page."""

    position_id: str
    symbol: str
    side: str
    entry_price: float
    volume: float
    current_price: float | None = None
    sl: float | None = None
    tp: float | None = None
    closed: bool = False
    opened_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        return cls(
            position_id=str(data["position_id"]),
            symbol=str(data["symbol"]),
            side=str(data["side"]),
            entry_price=float(data["entry_price"]),
            volume=float(data["volume"]),
            current_price=(
                float(data["current_price"])
                if data.get("current_price") is not None
                else None
            ),
            sl=float(data["sl"]) if data.get("sl") is not None else None,
            tp=float(data["tp"]) if data.get("tp") is not None else None,
            closed=bool(data.get("closed", False)),
            opened_at=data.get(
                "opened_at", datetime.now(timezone.utc).isoformat()
            ),
            closed_at=data.get("closed_at"),
        )


class PositionStore:
    """Read and write positions to a JSON file."""

    def __init__(self, path: Path | str = "positions.json") -> None:
        self.path = Path(path)
        self._positions: dict[str, Position] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._positions = {}
            return

        with self.path.open(encoding="utf-8") as handle:
            raw = json.load(handle)

        self._positions = {
            position_id: Position.from_dict(item)
            for position_id, item in raw.items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            position_id: position.to_dict()
            for position_id, position in self._positions.items()
        }
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

    def get_open_ids(self) -> set[str]:
        return {
            position_id
            for position_id, position in self._positions.items()
            if not position.closed
        }

    def upsert(self, position: Position) -> bool:
        """Insert or refresh a position. Returns True when new or updated."""
        is_new = position.position_id not in self._positions
        existing = self._positions.get(position.position_id)

        if existing and not is_new:
            position.opened_at = existing.opened_at
            position.closed = existing.closed
            position.closed_at = existing.closed_at
            updated = (
                existing.symbol != position.symbol
                or existing.side != position.side
                or existing.entry_price != position.entry_price
                or existing.current_price != position.current_price
                or existing.volume != position.volume
                or existing.sl != position.sl
                or existing.tp != position.tp
            )
            self._positions[position.position_id] = position
            return updated

        self._positions[position.position_id] = position
        return is_new

    def mark_closed(self, position_id: str) -> bool:
        position = self._positions.get(position_id)
        if position is None or position.closed:
            return False

        position.closed = True
        position.closed_at = datetime.now(timezone.utc).isoformat()
        return True


@dataclass
class PositionMapping:
    """Maps a source copy position to a destination API position."""

    source_position_id: str
    destination_position_id: int
    symbol: str
    side: str
    volume: float
    api_volume: int
    status: str = "open"
    opened_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PositionMapping:
        return cls(
            source_position_id=str(data["source_position_id"]),
            destination_position_id=int(data["destination_position_id"]),
            symbol=str(data["symbol"]),
            side=str(data["side"]),
            volume=float(data["volume"]),
            api_volume=int(data["api_volume"]),
            status=str(data.get("status", "open")),
            opened_at=data.get(
                "opened_at", datetime.now(timezone.utc).isoformat()
            ),
            closed_at=data.get("closed_at"),
        )


class MappingStore:
    """Persist source-to-destination position mappings."""

    def __init__(self, path: Path | str = "mapping.json") -> None:
        self.path = Path(path)
        self._mappings: dict[str, PositionMapping] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._mappings = {}
            return

        with self.path.open(encoding="utf-8") as handle:
            raw = json.load(handle)

        self._mappings = {
            source_id: PositionMapping.from_dict(item)
            for source_id, item in raw.items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            source_id: mapping.to_dict()
            for source_id, mapping in self._mappings.items()
        }
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

    def get(self, source_position_id: str) -> PositionMapping | None:
        return self._mappings.get(source_position_id)

    def has_open_mapping(self, source_position_id: str) -> bool:
        mapping = self._mappings.get(source_position_id)
        return mapping is not None and mapping.status == "open"

    def add(self, mapping: PositionMapping) -> None:
        self._mappings[mapping.source_position_id] = mapping

    def iter_open_mappings(self) -> list[PositionMapping]:
        return [
            mapping
            for mapping in self._mappings.values()
            if mapping.status == "open"
        ]

    def mark_closed(self, source_position_id: str) -> bool:
        mapping = self._mappings.get(source_position_id)
        if mapping is None or mapping.status == "closed":
            return False

        mapping.status = "closed"
        mapping.closed_at = datetime.now(timezone.utc).isoformat()
        return True
