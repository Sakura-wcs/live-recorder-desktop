from dataclasses import dataclass
from pathlib import Path

from .scan import snapshot


@dataclass(frozen=True)
class RecycleReport:
    recycled: tuple[Path, ...]
    retained: tuple[Path, ...]


def eligible(result):
    try:
        return (result.state == "success" and result.output is not None
                and snapshot(result.candidate.path) == result.candidate.snapshot
                and snapshot(result.output) == result.output_snapshot
                and result.output_snapshot.size < result.candidate.snapshot.size)
    except (OSError, ValueError, AttributeError):
        return False


def recycle_verified(results, mode, sink, cancel):
    results = list(results)
    if mode not in {"each", "batch"} or cancel.is_set() or (mode == "batch" and not all(map(eligible, results))):
        return RecycleReport((), tuple(item.candidate.path for item in results))
    recycled, retained = [], []
    stop = False
    for result in results:
        path = result.candidate.path
        permitted = not stop and not cancel.is_set() and eligible(result)
        try:
            moved = permitted and sink(path)
        except (OSError, ValueError, RuntimeError):
            moved = False
        (recycled if moved else retained).append(path)
        if not moved and mode == "batch":
            stop = True
    return RecycleReport(tuple(recycled), tuple(retained))


def windows_recycle(path: Path, *, recheck) -> bool:
    import os
    from PySide6.QtCore import QFile

    if os.name != "nt":
        return False
    from win32file import DRIVE_FIXED, GetDriveType

    if GetDriveType(path.anchor) != DRIVE_FIXED or not recheck():
        return False
    item = QFile(str(path))
    return item.moveToTrash()
