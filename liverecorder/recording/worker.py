import json
import os
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..paths import checked_path, safe_component, safe_join
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
                on_published=None, namespace=""):
    delay = interval
    while not stop.is_set():
        partial = None
        try:
            emit("checking", "正在检测开播")
            opened = open_stream()
            if opened is None:
                emit("offline", "暂未获取直播流，等待下次检测")
                delay = interval
                stop.wait(interval)
                continue
            stream, extension, title = opened
            with stream:
                if extension not in {".ts", ".flv", ".mp4", ".mkv"}:
                    raise ValueError("无法识别原始直播容器")
                if stop.is_set():
                    break
                directory = output_directory(root, name, platform, room_id, min_free_bytes=min_free_bytes,
                                             on_create=on_directory_created, namespace=namespace)
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                stem = f"{timestamp}_{uuid4().hex[:10]}_{safe_component(title)[:40]}"
                partial = safe_join(directory, stem + ".partial" + extension)
                target = safe_join(directory, stem + container)
                with partial.open("xb") as output:
                    while not stop.is_set():
                        try:
                            block = stream.read(256 * 1024)
                        except OSError:
                            emit("disconnected", "直播流中断，尝试保存已录制片段", partial)
                            break
                        if not block:
                            break
                        first = output.tell() == 0
                        output.write(block)
                        if first:
                            emit("recording", "录制中", partial)
                    output.flush()
                    original = os.fstat(output.fileno())
            if original.st_size:
                emit("finalizing", "正在无损封装", partial)
                published = finalize(partial, target, ffmpeg, ffprobe, on_published=on_published)
                # Only remove the private staging file created by this recording.
                try:
                    current = checked_path(partial).stat()
                    if (os.path.samestat(original, current) and current.st_size == original.st_size
                            and current.st_mtime_ns == original.st_mtime_ns):
                        partial.unlink()
                except (OSError, ValueError):
                    emit("retained", "已生成成品，未能清理本次录制中间文件", partial)
                emit("finished", "录制成品已写入", published)
            delay = interval
        except OutputSpaceError:
            emit("retrying", "录制盘空间不足，暂停开始新录像")
        except (OSError, RuntimeError, ValueError):
            emit("retrying", "录制或封装未完成，将重试", partial)
        if stop.wait(delay):
            break
        delay = min(max(interval, delay * 2), 300)
