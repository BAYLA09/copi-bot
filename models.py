from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Position:
    """A monitored XAUUSD position from the cTrader Copy investor page."""

    position_id: str
    symbol: str
    side: str
    entry_price: float
    volume: float
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
        """Insert or refresh a position. Returns True when it is new."""
        is_new = position.position_id not in self._positions
        existing = self._positions.get(position.position_id)

        if existing and not is_new:
            position.opened_at = existing.opened_at
            position.closed = existing.closed
            position.closed_at = existing.closed_at

        self._positions[position.position_id] = position
        return is_new

    def mark_closed(self, position_id: str) -> bool:
        position = self._positions.get(position_id)
        if position is None or position.closed:
            return False

        position.closed = True
        position.closed_at = datetime.now(timezone.utc).isoformat()
        return True
