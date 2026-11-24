import json
import os
import random
import shutil
import sqlite3
import sys
import threading
from time import monotonic
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..paths import checked_path, require_recording_handle, require_recording_volume, safe_component, safe_join
from .media import finalize


class OutputSpaceError(RuntimeError):
    pass


def output_directory(root: Path, name: str, platform: str, room_id: int, *, min_free_bytes: int,
                     on_create=None, namespace="") -> Path:
    root = checked_path(root)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < min_free_bytes:
        raise OutputSpaceError("录制盘可用空间不足")
    suffix = f"-{namespace}" if namespace else ""
    component = safe_component(f"{safe_component(name)[:35]} [{platform}-{room_id}{suffix}]")
    directory = safe_join(root, component)
    try:
        directory.mkdir()
    except FileExistsError:
        if not checked_path(directory).is_dir():
            raise
    else:
        if on_create is not None:
            on_create(checked_path(directory))
    return checked_path(directory)


def serve(room_id: int, capture) -> None:
    stop = threading.Event()

    def control():
        for command in sys.stdin:
            if command.strip() == "stop":
                break
        stop.set()

    def emit(state, message, path=None):
        print(json.dumps({"room_id": room_id, "state": state, "message": message,
                          "path": str(path) if path is not None else None}), flush=True)

    threading.Thread(target=control, daemon=True).start()
    capture(stop, emit)


def record_loop(stop, emit, open_stream, root, name, platform, room_id, ffmpeg, ffprobe,
                *, interval=15, min_free_bytes=512 * 1024**2, container=".mkv", on_directory_created=None,
                on_published=None, namespace="", journal=None):
    delay = interval
    while not stop.is_set():
        partial = None
        try:
            require_recording_volume(root)
            emit("checking", "正在检测开播")
            opened = open_stream()
            if opened is None:
                emit("offline", "未发现可用直播流（可能尚未开播），等待下次检测")
                delay = interval
                stop.wait(interval * random.uniform(1, 1.3))
                continue
            stream, extension, title = opened
            with stream:
                if extension not in {".ts", ".flv", ".mp4", ".mkv"}:
                    raise ValueError("无法识别原始直播容器")
                if stop.is_set():
                    break
                require_recording_volume(root)
                directory = output_directory(root, name, platform, room_id, min_free_bytes=min_free_bytes,
                                             on_create=on_directory_created, namespace=namespace)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                stem = f"{timestamp}_{uuid4().hex[:10]}_{safe_component(title)[:40]}"
                partial = safe_join(directory, stem + ".partial" + extension)
                target = safe_join(directory, stem + container)
                require_recording_volume(partial)
                with partial.open("xb") as output:
                    require_recording_handle(output, partial)
                    session = None
                    if journal:
                        with journal() as log:
                            session = log.begin(room_id, partial, target)
                    next_check = 0
                    disk_limited = False
                    while not stop.is_set():
                        try:
                            block = stream.read(256 * 1024)
                        except OSError:
                            emit("disconnected", "直播流中断，尝试保存已录制片段", partial)
                            break
                        if not block:
                            break
                        now = monotonic()
                        if now >= next_check:
                            require_recording_volume(directory)
                            if shutil.disk_usage(directory).free < min_free_bytes + output.tell() + len(block):
                                emit("disk_low", "录像盘余量不足，停止写入并尝试封装本段", partial)
                                disk_limited = True
                                break
                            next_check = now + 2
                        first = output.tell() == 0
                        output.write(block)
                        if first:
                            emit("recording", "录制中", partial)
                    output.flush()
                    original = os.fstat(output.fileno())
            if original.st_size:
                emit("finalizing", "正在无损封装", partial)
                require_recording_volume(target)
                if shutil.disk_usage(directory).free < original.st_size + min_free_bytes:
                    raise OutputSpaceError("录像盘空间不足，已保留未封装片段")

                def prepare(stamp):
                    require_recording_volume(target)
                    if journal:
                        with journal() as log:
                            log.prepare(session, stamp)

                def commit(path, stamp):
                    if journal:
                        with journal() as log:
                            log.complete(session, room_id, path, stamp)
                    elif on_published:
                        on_published(path, stamp)

                published = finalize(partial, target, ffmpeg, ffprobe, on_published=commit, before_publish=prepare,
                                     guard=require_recording_volume)
                # Only remove the private staging file created by this recording.
                try:
                    current = checked_path(partial).stat()
                    if (os.path.samestat(original, current) and current.st_size == original.st_size
                            and current.st_mtime_ns == original.st_mtime_ns):
                        partial.unlink()
                except (OSError, ValueError):
                    emit("retained", "已生成成品，未能清理本次录制中间文件", partial)
                emit("finished", "录制成品已写入", published)
            if disk_limited:
                raise OutputSpaceError("录像盘余量不足")
            delay = interval
        except OutputSpaceError:
            emit("disk_low", "录制盘空间不足，暂停开始新录像", partial)
        except PermissionError:
            emit("permission", "没有访问录像目录或平台凭据的权限", partial)
        except sqlite3.Error:
            emit("database", "录像库暂不可写，已保留磁盘文件", partial)
        except OSError:
            emit("io_error", "录像目录或录制工具暂不可访问，已保留磁盘文件", partial)
        except (RuntimeError, ValueError) as error:
            code = getattr(error, "code", None)
            emit(code or "retrying", str(error) if code else "录制或封装未完成，请检查媒体工具与原片", partial)
        if stop.wait(delay * random.uniform(1, 1.3)):
            break
        delay = min(max(interval, delay * 2), 300)
