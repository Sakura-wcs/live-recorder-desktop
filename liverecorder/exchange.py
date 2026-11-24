"""Room-list interchange and a recording inventory without absolute paths."""

import csv
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from unicodedata import category

from .paths import checked_path
from .platforms import normalize_room


_FORMAT = "liverecorder.rooms"
_MAX_BYTES = 1024 * 1024
_MAX_ROOMS = 2000
_PRIVATE_LOCATION = re.compile(r"://|[a-z]:[\\/]|\\\\|(?<![\w/\\])[/\\]\S", re.IGNORECASE)


def _text(value, maximum):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or not value.strip() or any(category(char) in {"Cc", "Cs"} for char in value)):
        raise ValueError("房间平台、ID 或主播名称格式无效。")
    return value.strip()


def _room(platform, identity, name):
    platform = _text(platform, 16)
    return platform, normalize_room(platform, _text(identity, 2048)), _text(name, 128)


def _shareable_name(name, identity):
    # Labels are free text; do not echo an accidentally pasted location or credential-bearing URL.
    return identity if _PRIVATE_LOCATION.search(name) else name


def _unique_rooms(rows):
    result = []
    seen = set()
    for index, row in enumerate(rows, 1):
        if index > _MAX_ROOMS:
            raise ValueError(f"房间文件最多支持 {_MAX_ROOMS} 项。")
        key = row[:2]
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _distinct_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("房间文件包含重复的 JSON 字段。")
    return result


def _read_rooms(path):
    with checked_path(Path(path)).open("rb") as file:
        data = file.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("房间文件不能超过 1 MiB。")
    try:
        document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_distinct_object)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("文件不是有效的 UTF-8 房间 JSON。") from error
    if (not isinstance(document, dict) or set(document) != {"format", "version", "rooms"}
            or document["format"] != _FORMAT or type(document["version"]) is not int
            or document["version"] != 1 or not isinstance(document["rooms"], list)):
        raise ValueError("不支持此房间文件格式或版本。")
    return document["rooms"]


def import_rooms(path):
    """Read version 1, validate strictly, and return first occurrence per canonical ID."""
    rows = _read_rooms(path)
    if len(rows) > _MAX_ROOMS:
        raise ValueError(f"房间文件最多支持 {_MAX_ROOMS} 项。")
    entries = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != {"platform", "identity", "name"}:
            raise ValueError(f"第 {index} 项的房间字段无效。")
        try:
            entries.append(_room(row["platform"], row["identity"], row["name"]))
        except ValueError as error:
            raise ValueError(f"第 {index} 项的房间信息无效。") from error
    return _unique_rooms(entries)


@contextmanager
def _output(path, *, encoding, overwrite):
    target = checked_path(Path(path))
    if not overwrite and target.exists():
        raise FileExistsError("导出文件已存在。")
    staging = None
    try:
        with NamedTemporaryFile("w", encoding=encoding, newline="", dir=target.parent,
                                prefix=".liverecorder-export-", delete=False) as file:
            staging = Path(file.name)
            yield file
            file.flush()
            os.fsync(file.fileno())
        staged, destination = checked_path(staging), checked_path(target)
        if overwrite:
            staged.replace(destination)
        elif os.name == "nt":
            staged.rename(destination)  # Windows rename fails if the destination already exists.
        else:
            os.link(staged, destination)
    finally:
        if staging is not None:
            checked_path(staging).unlink(missing_ok=True)


def export_rooms(path, rooms, *, overwrite=False):
    """Export only platform, canonical identity, and display name; return the row count."""
    rows = _unique_rooms(_room(room.platform, room.identity, room.name) for room in rooms)
    data = json.dumps({"format": _FORMAT, "version": 1,
                       "rooms": [{"platform": key, "identity": identity, "name": _shareable_name(name, identity)}
                                 for key, identity, name in rows]}, ensure_ascii=False, indent=2) + "\n"
    if len(data.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("房间文件不能超过 1 MiB。")
    with _output(path, encoding="utf-8", overwrite=overwrite) as file:
        file.write(data)
    return len(rows)


def _csv_text(value):
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")):
        return "'" + value
    return value


def export_recordings(path, rooms, recordings, *, overwrite=False):
    """Write an Excel-readable UTF-8 CSV with basenames, never recording directories."""
    by_id = {room.id: room for room in rooms}
    count = 0
    with _output(path, encoding="utf-8-sig", overwrite=overwrite) as file:
        writer = csv.writer(file, quoting=csv.QUOTE_ALL)
        writer.writerow(("平台", "房间 ID", "主播", "文件名", "状态"))
        for recording in recordings:
            room = by_id.get(recording.room_id)
            if room:
                identity = normalize_room(room.platform, room.identity)
                label = (room.platform, identity, _shareable_name(room.name, identity))
            else:
                label = ("", "", "")
            status = "已保存" if recording.is_current() else "失联、变更或未登记"
            writer.writerow(_csv_text(value) for value in (*label, recording.path.name, status))
            count += 1
    return count
