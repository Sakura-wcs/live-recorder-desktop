import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .paths import checked_path
from .platforms import HOSTS, normalize_room


@dataclass(frozen=True)
class ImportRow:
    line: int
    raw: str
    platform: str = ""
    identity: str = ""
    status: str = ""


def import_rows(text, existing):
    hosts = {alias: platform for platform, host in HOSTS.items()
             for alias in (host, host.removeprefix("www."))}
    hosts["youtu.be"] = "youtube"
    seen = set(existing)
    rows = []
    for i, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            platform = hosts[urlsplit(raw).netloc.lower()]
            identity = normalize_room(platform, raw)
        except (ValueError, KeyError):
            rows.append(ImportRow(i, raw, status="无法识别链接"))
            continue
        key = platform, identity
        rows.append(ImportRow(i, raw, *key, "重复" if key in seen else ""))
        seen.add(key)
    return rows


@dataclass(frozen=True)
class Removal:
    room: int
    path: Path
    stamp: tuple
    files: tuple

    @property
    def size(self):
        return sum(stamp[2] for _, stamp in self.files)


def _directory_stamp(info):
    return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", 0)


def _file_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def inventory(room, path, stamp):
    path = checked_path(path)
    if path == path.parent or stat.S_ISDIR(path.lstat().st_mode) is False or not _same_directory(path, stamp):
        raise ValueError("目录身份已变化")
    files = []
    for child in sorted(path.iterdir()):
        info = checked_path(child).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("目录包含子目录、特殊条目或硬链接")
        files.append((child, _file_stamp(info)))
    return Removal(room, path, stamp, tuple(files))


def _same_directory(path, stamp):
    info = checked_path(path).lstat()
    return stat.S_ISDIR(info.st_mode) and bool(info.st_ino) and _directory_stamp(info) == stamp


def plan_removal(db, rooms, *, protected=()):
    rooms = set(rooms)
    plans = []
    skipped = {room: set() for room in rooms}
    registered = db.owned_directories()
    records = db.recordings()
    known = {(item.room_id, item.path): item.original for item in records if item.published}
    for room, path, stamp in registered:
        if room not in rooms:
            continue
        try:
            if (any(place.is_relative_to(path) or path.is_relative_to(place) for place in protected)
                    or any(place != path and (place.is_relative_to(path) or path.is_relative_to(place))
                           for _, place, _ in registered)
                    or any(item.room_id != room and item.path.is_relative_to(path) for item in records)):
                raise ValueError("目录不独占")
            plan = inventory(room, path, stamp)
            if any(known.get((room, file)) != stamp for file, stamp in plan.files):
                raise ValueError("存在未登记或身份已变化的文件")
            plans.append(plan)
        except (OSError, ValueError):
            skipped[room].add(path)
    for record in records:
        if record.room_id in rooms and not any(
            plan.room == record.room_id and record.path.is_relative_to(plan.path) for plan in plans
        ):
            skipped[record.room_id].add(record.path.parent)
    return plans, skipped


def remove_directory(plan):
    if os.name != "nt":
        raise ValueError("永久删除仅支持 Windows")
    import ctypes
    from ctypes import wintypes

    import pywintypes
    import win32con
    import win32file

    disposition = ctypes.WinDLL("kernel32", use_last_error=True).SetFileInformationByHandle
    disposition.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    disposition.restype = wintypes.BOOL
    delete = ctypes.c_bool(True)
    list_directory = 0x0001  # FILE_LIST_DIRECTORY

    def pin(stack, path, access, share):
        handle = win32file.CreateFile(
            str(checked_path(path)), access, share, None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32file.FILE_FLAG_BACKUP_SEMANTICS, None)
        stack.callback(handle.Close)
        if win32file.GetFileInformationByHandle(handle)[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("路径已变成重解析点")
        return handle

    def mark_for_deletion(handle):
        if not disposition(int(handle), win32file.FileDispositionInfo, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        with ExitStack() as directories:
            # Deny rename/deletion on every ancestor before opening its child by pathname.
            for parent in reversed(plan.path.parents):
                pin(directories, parent, list_directory, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE)
            directory = pin(directories, plan.path, win32con.DELETE, win32con.FILE_SHARE_READ)
            with ExitStack() as files:
                handles = [pin(files, path, win32con.DELETE, win32con.FILE_SHARE_READ) for path, _ in plan.files]
                if inventory(plan.room, plan.path, plan.stamp) != plan:
                    raise ValueError("预览后目录内容已变化，未删除")
                for handle in handles:
                    mark_for_deletion(handle)
            # An unexpected new file makes the directory non-empty; never open or remove it.
            mark_for_deletion(directory)
    except pywintypes.error as error:
        raise OSError("无法锁定录像目录或文件") from error
