import json
import stat
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from ..paths import checked_path

OUTPUT_FOLDER = "LiveRecorder_压缩成品"
MEDIA_SUFFIXES = {".mp4", ".mkv", ".ts", ".flv", ".mov", ".webm", ".m4v", ".avi", ".m2ts", ".wmv", ".mpg", ".mts"}


@dataclass(frozen=True)
class Snapshot:
    device: int
    identifier: int
    size: int
    modified: int


@dataclass(frozen=True)
class Candidate:
    path: Path
    snapshot: Snapshot | None
    state: str
    media: dict | None = None

    @property
    def video(self):
        return next((item for item in (self.media or {}).get("streams", [])
                     if item["codec_type"] == "video" and not item.get("disposition", {}).get("attached_pic")), {})

    def classify(self, target):
        if self.media is None:
            return self
        return replace(self, state="same" if self.video["codec_name"] == target else "ready")


def snapshot(path):
    info = checked_path(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("不是普通文件")
    return Snapshot(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def inspect(path, ffprobe, *, count_frames=False):
    checks = ["-count_frames", "-count_packets"] if count_frames else []
    process = subprocess.run(
        [str(ffprobe), "-v", "error", *checks, "-show_streams", "-show_format", "-of", "json", str(checked_path(path))],
        capture_output=True, check=True, timeout=600 if count_frames else 18,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.stderr:
        raise ValueError("媒体检查报告错误")
    return json.loads(process.stdout)


def _inspect(path, info, target, ffprobe):
    if ".partial" in path.name.casefold():
        return Candidate(path, info, "unfinished")
    try:
        media = inspect(path, ffprobe)
        videos = [item for item in media["streams"] if item["codec_type"] == "video"
                  and not item.get("disposition", {}).get("attached_pic")]
        if len(videos) != 1:
            return Candidate(path, info, "invalid")
        return Candidate(path, info, "ready", media).classify(target)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return Candidate(path, info, "invalid")


def _files(paths, recursive, cancel):
    for path in paths:
        if cancel.is_set():
            break
        try:
            path = checked_path(Path(path))
            if not path.is_dir():
                yield path
                continue
            pending = [path]
            while pending and not cancel.is_set():
                current = checked_path(pending.pop())
                for entry in current.iterdir():
                    if cancel.is_set():
                        break
                    try:
                        checked_path(entry)
                        if entry.is_dir():
                            if recursive and entry.name.casefold() != OUTPUT_FOLDER.casefold():
                                pending.append(entry)
                        elif entry.suffix.lower() in MEDIA_SUFFIXES:
                            yield entry
                    except (ValueError, OSError):
                        continue
        except (ValueError, OSError):
            continue


def scan(paths, target, ffprobe, cancel, *, recursive=True, expected=None):
    seen = set()
    pending = deque()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for path in _files(paths, recursive, cancel):
            if cancel.is_set():
                break
            try:
                info = snapshot(path)
            except (OSError, ValueError):
                yield Candidate(path, None, "invalid")
                continue
            if expected is not None and expected.get(path) != (info.device, info.identifier, info.size, info.modified):
                yield Candidate(path, None, "invalid")
                continue
            key = (info.device, info.identifier) if info.identifier else path
            if key in seen:
                continue
            seen.add(key)
            pending.append(pool.submit(_inspect, path, info, target, ffprobe))
            if len(pending) == 4:
                yield pending.popleft().result()
        while pending and not cancel.is_set():
            yield pending.popleft().result()
        for future in pending:
            future.cancel()
