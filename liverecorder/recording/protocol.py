from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RoomEvent:
    room_id: int
    state: str
    message: str
    path: Path | None = None
