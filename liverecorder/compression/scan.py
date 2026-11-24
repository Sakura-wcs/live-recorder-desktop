import json
import os
import stat
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic

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
    message: str = ""

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


class ProbeCancelled(RuntimeError):
    pass


def inspect(path, ffprobe, *, count_frames=False, cancel=None):
    if cancel is not None and cancel.is_set():
        raise ProbeCancelled("已取消媒体检查")
    checks = ["-count_frames", "-count_packets"] if count_frames else []
    command = [str(ffprobe), "-v", "error", *checks, "-show_streams", "-show_format", "-of", "json", str(checked_path(path))]
    timeout = 600 if count_frames else 18
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        deadline = monotonic() + timeout
        while True:
            try:
                if cancel is not None and cancel.is_set():
                    raise ProbeCancelled("已取消媒体检查")
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                if monotonic() < deadline:
                    continue
                process.kill()
                process.communicate()
                raise subprocess.TimeoutExpired(command, timeout) from None
            except ProbeCancelled:
                process.kill()
                process.communicate()
                raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    if stderr:
        raise ValueError("媒体检查报告错误")
    return json.loads(stdout)


def _inspect(path, info, target, ffprobe, cancel):
    if ".partial" in path.name.casefold():
        return Candidate(path, info, "unfinished")
    try:
        media = inspect(path, ffprobe, cancel=cancel)
        videos = [item for item in media["streams"] if item["codec_type"] == "video"
                  and not item.get("disposition", {}).get("attached_pic")]
        if len(videos) != 1:
            return Candidate(path, info, "invalid", message="需要且仅支持一条普通视频轨")
        return Candidate(path, info, "ready", media).classify(target)
    except ProbeCancelled:
        return None
    except subprocess.TimeoutExpired:
        return Candidate(path, info, "invalid", message="媒体检查超时")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return Candidate(path, info, "invalid", message="无法读取媒体信息或格式不受支持")


def _files(paths, recursive, cancel):
    for source in paths:
        if cancel.is_set():
            break
        path = Path(source)
        try:
            if not stat.S_ISDIR(checked_path(path).lstat().st_mode):
                yield path, None
                continue
        except (ValueError, OSError):
            yield path, "来源不存在、不可访问或含不受支持的链接"
            continue
        pending = [path]
        while pending and not cancel.is_set():
            current = pending.pop()
            try:
                with os.scandir(checked_path(current)) as entries:
                    for entry in entries:
                        if cancel.is_set():
                            break
                        candidate = Path(entry.path)
                        try:
                            checked_path(candidate)
                            if entry.is_dir(follow_symlinks=False):
                                if recursive and entry.name.casefold() != OUTPUT_FOLDER.casefold():
                                    pending.append(candidate)
                            elif candidate.suffix.lower() in MEDIA_SUFFIXES:
                                yield candidate, None
                        except (ValueError, OSError):
                            yield candidate, "条目不可访问或含不受支持的链接"
            except (ValueError, OSError):
                yield current, "无法读取目录"


def scan(paths, target, ffprobe, cancel, *, recursive=True, expected=None):
    seen, pending = set(), set()
    files = iter(_files(paths, recursive, cancel))
    more = True
    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while not cancel.is_set() and (more or pending):
                while more and len(pending) < 4 and not cancel.is_set():
                    entry = next(files, None)
                    if entry is None:
                        more = False
                        break
                    path, problem = entry
                    if problem is not None:
                        yield Candidate(path, None, "inaccessible", message=problem)
                        continue
                    try:
                        info = snapshot(path)
                    except (OSError, ValueError):
                        yield Candidate(path, None, "inaccessible", message="无法读取文件")
                        continue
                    if expected is not None and expected.get(path) != (info.device, info.identifier, info.size, info.modified):
                        yield Candidate(path, None, "invalid", message="录像身份已变化或未核实")
                        continue
                    key = (info.device, info.identifier) if info.identifier else path
                    if key in seen:
                        continue
                    seen.add(key)
                    pending.add(pool.submit(_inspect, path, info, target, ffprobe, cancel))
                if pending and not cancel.is_set():
                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        candidate = future.result()
                        if candidate is not None and not cancel.is_set():
                            yield candidate
        finally:
            files.close()
            for future in pending:
                future.cancel()
