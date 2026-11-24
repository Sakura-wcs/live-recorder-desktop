"""Durable capture intent; recovery never removes media."""

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..catalog import file_identity
from ..paths import checked_path, require_recording_volume
from .media import finalize


@contextmanager
def _pin_source(path):
    if os.name != "nt":
        yield
        return
    import pywintypes
    import win32con
    import win32file

    try:
        handle = win32file.CreateFile(str(checked_path(path)), win32con.GENERIC_READ, win32con.FILE_SHARE_READ,
                                      None, win32con.OPEN_EXISTING, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
    except pywintypes.error as error:
        raise OSError("无法锁定原始片段，请关闭其他使用它的程序") from error
    try:
        yield
    finally:
        handle.Close()


def source_key(path):
    try:
        info = checked_path(path).lstat()
        if stat.S_ISREG(info.st_mode) and info.st_ino and info.st_nlink == 1:
            return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", 0)
    except (OSError, ValueError):
        pass
    return None


@dataclass(frozen=True)
class Session:
    id: int
    room_id: int
    raw: Path
    target: Path
    raw_stamp: tuple
    output_stamp: tuple | None


class RecordingJournal:
    def __init__(self, catalog):
        self.catalog = catalog
        self.db = catalog.db

    def begin(self, room_id, raw, target):
        key = source_key(raw)
        if not key or raw.parent != target.parent or ".partial" not in raw.name:
            raise ValueError("无法登记录制片段")
        with self.db:
            return self.db.execute(
                "INSERT INTO recording_sessions(room_id,raw,target,raw_stamp) VALUES(?,?,?,?) RETURNING id",
                (room_id, str(raw), str(target), json.dumps(key)),
            ).fetchall()[0][0]

    def prepare(self, session, stamp):
        with self.db:
            self.db.execute("UPDATE recording_sessions SET output_stamp=?,state='prepared' WHERE id=?",
                            (json.dumps(stamp), session))

    def complete(self, session, room_id, target, stamp):
        self.catalog.add_recording(room_id, target, published_stamp=stamp)
        with self.db:
            self.db.execute("UPDATE recording_sessions SET state='finished' WHERE id=?", (session,))

    def pending(self):
        return [Session(row[0], row[1], Path(row[2]), Path(row[3]), tuple(json.loads(row[4])),
                        tuple(json.loads(row[5])) if row[5] else None)
                for row in self.db.execute(
                    "SELECT id,room_id,raw,target,raw_stamp,output_stamp FROM recording_sessions "
                    "WHERE state IN ('capturing','prepared') ORDER BY id DESC")]

    def reconcile(self):
        count = 0
        for item in self.pending():
            if item.output_stamp and file_identity(item.target) == item.output_stamp:
                self.complete(item.id, item.room_id, item.target, item.output_stamp)
                count += 1
        return count

    @staticmethod
    def _retryable(item):
        try:
            return (not checked_path(item.target).exists()
                    and source_key(item.raw) == item.raw_stamp and item.raw.stat().st_size > 0)
        except (OSError, ValueError):
            return False

    def retryable(self):
        return [item for item in self.pending() if self._retryable(item)]

    def recover(self, item, ffmpeg, ffprobe):
        require_recording_volume(item.raw)
        require_recording_volume(item.target)
        with _pin_source(item.raw):
            stamp = file_identity(item.raw)
            if not self._retryable(item) or not stamp or stamp[:2] != item.raw_stamp[:2]:
                raise ValueError("片段身份无法验证，或成品已经存在")

            def prepare(output_stamp):
                require_recording_volume(item.target)
                if file_identity(item.raw) != stamp:
                    raise ValueError("原片在封装期间发生变化")
                self.prepare(item.id, output_stamp)

            return finalize(item.raw, item.target, ffmpeg, ffprobe, before_publish=prepare,
                            on_published=lambda path, output_stamp: self.complete(item.id, item.room_id, path, output_stamp),
                            guard=require_recording_volume)
