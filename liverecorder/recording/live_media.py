"""Bounded, file-free input remuxing into a caller-owned partial MKV."""

import json
import math
import os
import stat
import subprocess
import threading
import time
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

from ..paths import require_recording_handle, require_recording_volume
from .media import RemuxError

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_WRITE_TIMEOUT = 15
_FINISH_TIMEOUT = 15
_KILL_TIMEOUT = 2
_PROBE_TIMEOUT = 5


@lru_cache(maxsize=4)
def require_live_mkv_tool(ffmpeg):
    try:
        result = subprocess.run([str(ffmpeg), "-hide_banner", "-protocols"], capture_output=True,
                                check=True, timeout=5, creationflags=_CREATE_NO_WINDOW)
        if b"fd" not in result.stdout.partition(b"Output:")[2].split():
            raise ValueError("missing output fd protocol")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RemuxError("MKV 实时封装需要支持 fd: 输出协议的 FFmpeg，请更新媒体工具") from error


class LiveMkv:
    """Feed one continuous response, then either finish or abort from the owning thread."""

    def __init__(self, partial: Path, extension: str, ffmpeg: Path, *, guard=require_recording_volume):
        self._processes = []
        self._condition = threading.Condition()
        self._deadline = None
        self._timed_out = False
        self._closed = False
        self._watchdog = None
        self._partial = Path(partial)
        self._guard = guard
        extension = extension.lower()
        if extension not in {".ts", ".flv", ".mkv", ".mp4"}:
            raise RemuxError("不支持的直播流格式")
        base = [str(ffmpeg), "-nostdin", "-v", "error", "-xerror"]
        mapping = ["-map", "0:V", "-map", "0:a?", "-c", "copy", "-f", "matroska"]
        options = dict(stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW, bufsize=0)
        try:
            self._guard(self._partial)
            with self._partial.open("r+b") as output:
                require_recording_handle(output, self._partial)
                self._opened = os.fstat(output.fileno())
                if (self._opened.st_size or not stat.S_ISREG(self._opened.st_mode)
                        or not os.path.samestat(self._opened, self._partial.stat())):
                    raise ValueError("录像暂存文件已变化")
                if extension == ".flv":
                    # Live FLV can deliver AAC extradata after the initial header.
                    # The streaming pass avoids rewriting a seekable header with
                    # that extradata; the second writer builds duration and cues.
                    upstream = subprocess.Popen(
                        [*base, "-f", "flv", "-i", "pipe:0", *mapping, "-live", "1", "pipe:1"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, **options,
                    )
                    self._processes.append(upstream)
                    source = upstream.stdout
                    input_format = ["-f", "matroska"]
                else:
                    source = subprocess.PIPE
                    input_format = []  # HLS may carry TS or fragmented MP4.
                try:
                    writer = subprocess.Popen(
                        [*base, *input_format, "-i", "pipe:0", *mapping, "-fd", "1", "fd:"],
                        stdin=source, stdout=output, **options,
                    )
                    self._processes.append(writer)
                finally:
                    if extension == ".flv":
                        source.close()  # Only the downstream may hold the pipe read handle.
            self._watchdog = threading.Thread(target=self._watch_writes, daemon=True)
            self._watchdog.start()
        except (OSError, ValueError, RuntimeError) as error:
            self.abort()
            raise RemuxError("无法启动直播封装，已保留暂存文件") from error

    def _kill(self):
        # Signal both before waiting: either side of the FLV pipe may be blocked.
        for process in self._processes:
            with suppress(OSError):
                if process.poll() is None:
                    process.kill()

    def _watch_writes(self):
        with self._condition:
            while not self._closed:
                if self._deadline is None:
                    self._condition.wait()
                    continue
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                self._timed_out = True
                self._kill()
                return

    def write(self, data: bytes):
        with self._condition:
            if self._closed:
                raise RemuxError("直播封装已关闭")
            self._deadline = time.monotonic() + _WRITE_TIMEOUT
            self._condition.notify()
        try:
            start = self._processes[0]
            if any(process.poll() is not None for process in self._processes):
                raise BrokenPipeError("FFmpeg 已退出")
            pending = memoryview(data)
            while pending:
                written = start.stdin.write(pending)
                if not written:
                    raise BrokenPipeError("无法写入 FFmpeg 管道")
                pending = pending[written:]
        except OSError as error:
            self.abort()
            message = "直播封装写入超时" if self._timed_out else "直播封装写入失败"
            raise RemuxError(f"{message}，已保留暂存文件") from error
        finally:
            with self._condition:
                self._deadline = None
                self._condition.notify()
        if self._timed_out:
            self.abort()
            raise RemuxError("直播封装写入超时，已保留暂存文件")

    def _close_input(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self._processes:
            with suppress(OSError):
                self._processes[0].stdin.close()
        if self._watchdog is not None and self._watchdog.ident is not None:
            self._watchdog.join(timeout=_KILL_TIMEOUT)

    def finish(self):
        if self._closed:
            raise RemuxError("直播封装已关闭")
        self._close_input()
        deadline = time.monotonic() + _FINISH_TIMEOUT
        try:
            for process in self._processes:
                if process.wait(timeout=max(0, deadline - time.monotonic())):
                    raise RemuxError("FFmpeg 无法完成直播封装，已保留暂存文件")
            self._guard(self._partial)
            final = self._partial.stat()
            if not final.st_size or not os.path.samestat(self._opened, final):
                raise RemuxError("未生成直播录像或暂存文件已变化")
        except subprocess.TimeoutExpired as error:
            raise RemuxError("直播封装收尾超时，已保留暂存文件") from error
        except (OSError, ValueError) as error:
            raise RemuxError("直播录像不可读，已保留暂存文件") from error
        finally:
            self.abort()

    def abort(self):
        self._kill()
        self._close_input()
        deadline = time.monotonic() + _KILL_TIMEOUT
        for process in self._processes:
            with suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=max(0, deadline - time.monotonic()))


def _probe(ffprobe, path, *args):
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-probesize", "1000000", "-analyzeduration", "2000000",
         *args, "-of", "json", str(path)],
        capture_output=True, check=True, timeout=_PROBE_TIMEOUT, creationflags=_CREATE_NO_WINDOW,
    )
    if result.stderr:
        raise ValueError("FFprobe 报告媒体错误")
    return json.loads(result.stdout)


def _sample(ffprobe, path, interval, tracks):
    sample = _probe(ffprobe, path, "-read_intervals", interval, "-show_packets",
                    "-show_entries", "packet=stream_index,pts_time,duration_time")
    ends = {}
    for packet in sample["packets"]:
        track = packet["stream_index"]
        timestamp = float(packet["pts_time"])
        raw_duration = packet.get("duration_time", "N/A")
        duration = 0.0 if raw_duration == "N/A" else float(raw_duration)
        if track not in tracks or not math.isfinite(timestamp + duration) or duration < 0:
            raise ValueError("直播录像时间戳无效")
        ends[track] = max(ends.get(track, -math.inf), timestamp + duration)
    if ends.keys() != tracks:
        raise ValueError("无法抽查所有直播轨道")
    return max(ends.values())


def quick_check(path: Path, ffprobe: Path):
    """Check indexed Matroska metadata and bounded packet samples near both ends."""
    try:
        path = require_recording_volume(path)
        metadata = _probe(ffprobe, path, "-show_entries",
                          "format=format_name,duration,start_time:stream=index,codec_type,codec_name,width,height")
        container = metadata["format"]
        duration = float(container["duration"])
        start = float(container["start_time"])
        if ("matroska" not in container["format_name"].split(",")
                or not math.isfinite(start + duration) or duration <= 0):
            raise ValueError("录像容器或时长无效")
        streams = metadata["streams"]
        video = [stream for stream in streams if stream["codec_type"] == "video"]
        if (not video or any(stream["width"] <= 0 or stream["height"] <= 0 for stream in video)
                or any(stream["codec_type"] not in {"video", "audio"} or not stream.get("codec_name")
                       for stream in streams)):
            raise ValueError("录像缺少有效的音视频轨道")
        tracks = {stream["index"] for stream in streams}
        _sample(ffprobe, path, "%+#256", tracks)
        end = start + duration
        # Seeking lands at an earlier cue. Leave a fixed packet budget for long GOPs.
        tail = _sample(ffprobe, path, f"{max(start, end - 2):.6f}%+#8192", tracks)
        if abs(tail - end) > 0.75:
            raise ValueError("录像尾部与容器时长不符")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise RemuxError("直播录像快速校验未通过，已保留暂存文件") from error
