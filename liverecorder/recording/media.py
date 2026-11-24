import json
import math
import os
import subprocess
from contextlib import suppress
from fractions import Fraction
from pathlib import Path
from threading import Timer
from uuid import uuid4

from ..catalog import file_identity
from ..paths import checked_path


class RemuxError(RuntimeError):
    """A short, safe message for the recording status and recovery dialog."""


def _media_summary(path, ffprobe, *, count_packets=True):
    count = ["-count_packets"] if count_packets else []
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", *count, "-show_format", "-show_streams",
         "-of", "json", str(checked_path(path))],
        capture_output=True, timeout=300, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.stderr:
        raise ValueError("媒体探测报告错误")
    data = json.loads(completed.stdout)
    streams = tuple(
        (item["codec_type"], item.get("codec_name"), item.get("width"), item.get("height"),
         item.get("sample_rate"), item.get("channels"), item.get("nb_read_packets"),
         Fraction(item["r_frame_rate"]) if item["codec_type"] == "video" else None)
        for item in data["streams"]
    )
    if not any(item[0] == "video" for item in streams):
        raise ValueError("未识别到视频流")
    if not count_packets:
        return streams, None  # FLV validation uses packet timestamps, not the container duration.
    duration = float(data["format"]["duration"])
    if not 0 < duration < float("inf"):
        raise ValueError("无法验证录像时长")
    return streams, duration


def _packet_timeline(path, ffprobe):
    """Check packet counts and measure the presentation interval in one bounded pass."""
    command = [str(ffprobe), "-v", "error", "-show_packets", "-show_entries", "packet=stream_index,pts_time,duration_time",
               "-of", "compact=p=0:nk=0", str(checked_path(path))]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="ascii",
                          errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as probe:
        def stop():
            with suppress(OSError):
                if probe.poll() is None:
                    probe.kill()

        timer = Timer(300, stop)
        timer.daemon = True
        timer.start()
        try:
            tracks = {}
            for line in probe.stdout:
                fields = dict(part.split("=", 1) for part in line.strip().split("|") if "=" in part)
                if "pts_time" not in fields:
                    raise ValueError("无法读取封装时间戳")
                pts = float(fields["pts_time"])
                stream = int(fields["stream_index"])
                raw_duration = fields.get("duration_time", "N/A")
                duration = 0.0 if raw_duration == "N/A" else float(raw_duration)
                if not math.isfinite(pts) or not math.isfinite(duration) or duration < 0:
                    raise ValueError("封装时间戳无效")
                count, low, high = tracks.get(stream, (0, math.inf, -math.inf))
                tracks[stream] = count + 1, min(low, pts), max(high, pts + duration)
            if probe.wait() != 0 or not tracks:
                raise ValueError("无法核实封装实际时长")
            return tracks
        finally:
            timer.cancel()
            stop()


def _close_duration(source, target):
    return abs(source - target) <= min(2, max(0.5, source * 0.001))


def validate_remux(source: Path, target: Path, ffprobe: Path) -> None:
    flv = source.suffix.lower() == ".flv"
    target_tracks, target_duration = _media_summary(target, ffprobe, count_packets=not flv)
    source_tracks, source_duration = _media_summary(source, ffprobe, count_packets=not flv)
    if source_tracks != target_tracks:
        raise ValueError("无损封装的轨道、编码或包数未通过检查")
    if flv:
        source_packets = _packet_timeline(source, ffprobe)
        target_packets = _packet_timeline(target, ffprobe)
        if (set(source_packets) != set(range(len(source_tracks))) or set(target_packets) != set(source_packets)
                or any(source_packets[i][0] != target_packets[i][0] for i in source_packets)):
            raise ValueError("无损封装的包数未通过检查")
        start_source = min(item[1] for item in source_packets.values())
        start_target = min(item[1] for item in target_packets.values())
        source_duration = max(item[2] for item in source_packets.values()) - start_source
        target_duration = max(item[2] for item in target_packets.values()) - start_target
        if any(abs((source_packets[i][slot] - start_source) - (target_packets[i][slot] - start_target)) > 0.1
               for i in source_packets for slot in (1, 2)):
            raise ValueError("无损封装的音视频轨道时间轴未通过检查")
        if not 0 < source_duration < math.inf or not 0 < target_duration < math.inf:
            raise ValueError("无法核实封装实际时长")
    if _close_duration(source_duration, target_duration):
        return
    raise ValueError("无损封装的实际时长未通过检查")


def probe_video(path: Path, ffprobe: Path) -> tuple:
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "v", "-show_streams", "-of", "json", str(checked_path(path))],
        capture_output=True, timeout=30, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    streams = [
        (item["codec_name"], item["width"], item["height"], Fraction(item["r_frame_rate"]))
        for item in json.loads(completed.stdout)["streams"]
        if not item.get("disposition", {}).get("attached_pic")
    ]
    if not streams:
        raise ValueError("未识别到视频流")
    return tuple(streams)


def _copy_streams(source, staging, ffmpeg, container, *, salvage=False):
    error_policy = ["-fflags", "+discardcorrupt"] if salvage else ["-xerror"]
    base = [str(ffmpeg), "-nostdin", "-v", "error", *error_policy, "-y"]
    mapping = ["-map", "0", "-c", "copy", "-f", container]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if source.suffix.lower() == ".flv" and container == "matroska":
        # The seekable Matroska writer can corrupt its headers on in-band AAC
        # extradata from live FLV. A streamable pass carries the payloads into
        # a second writer which builds a normal indexed MKV, with no extra disk copy.
        command = [*base, "-i", str(source), *mapping, "-live", "1", "pipe:1"]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags) as upstream:
            try:
                subprocess.run([*base, "-f", "matroska", "-i", "pipe:0", *mapping, str(staging)],
                               stdin=upstream.stdout, capture_output=True, timeout=600, check=True, creationflags=flags)
                if status := upstream.wait(timeout=10):
                    raise subprocess.CalledProcessError(status, command)
            finally:
                if upstream.poll() is None:
                    upstream.kill()
        return
    args = [*base, "-i", str(source), *mapping]
    if container == "mp4":
        args.extend(["-movflags", "+faststart"])
    subprocess.run([*args, str(staging)], capture_output=True, timeout=600, check=True, creationflags=flags)


def finalize(source: Path, target: Path, ffmpeg: Path, ffprobe: Path, *, on_published=None, before_publish=None,
             guard=checked_path, salvage_check=None) -> Path:
    source, target = checked_path(source), checked_path(target)
    formats = {".mkv": "matroska", ".mp4": "mp4"}
    if source == target or target.suffix.lower() not in formats:
        raise ValueError("无效的成品路径或容器")
    if target.exists():
        raise FileExistsError(target)
    staging = checked_path(target.with_name(f".liverecorder-{uuid4().hex}.partial"))
    owned = False
    try:
        guard(staging).touch(exist_ok=False)
        owned = True
        _copy_streams(source, guard(staging), ffmpeg, formats[target.suffix.lower()], salvage=salvage_check is not None)
        stamp = file_identity(staging)
        try:
            if salvage_check is None:
                validate_remux(source, staging, ffprobe)
            else:
                salvage_check(staging, ffprobe)
        except (ValueError, KeyError) as error:
            raise RemuxError("成品校验未通过，已保留原片") from error
        if stamp is None or file_identity(staging) != stamp:
            raise ValueError("无法校验封装成品身份")
        if before_publish:
            before_publish(stamp)
        if file_identity(staging) != stamp:
            raise ValueError("待发布成品已变化")
        guard(target)
        if os.name == "nt":
            guard(staging).rename(guard(target))  # Windows does not replace an existing destination.
        else:
            os.link(checked_path(staging), target)
        if on_published is not None:
            on_published(target, stamp)
        return target
    except FileExistsError:
        raise
    except subprocess.TimeoutExpired as error:
        raise RemuxError("封装或验证超时，已保留原片") from error
    except subprocess.CalledProcessError as error:
        raise RemuxError("FFmpeg 或 FFprobe 处理失败，已保留原片") from error
    except (OSError, ValueError, KeyError) as error:
        raise RemuxError("封装未完成，已保留原片") from error
    finally:
        if owned:
            checked_path(staging).unlink(missing_ok=True)
