import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

from ..catalog import file_identity
from ..paths import checked_path


def _media_summary(path, ffprobe):
    completed = subprocess.run(
        [str(ffprobe), "-v", "error", "-count_packets", "-show_format", "-show_streams",
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
    duration = float(data["format"]["duration"])
    if not 0 < duration < float("inf"):
        raise ValueError("无法验证录像时长")
    return streams, duration


def validate_remux(source: Path, target: Path, ffprobe: Path) -> None:
    source_tracks, source_duration = _media_summary(source, ffprobe)
    target_tracks, target_duration = _media_summary(target, ffprobe)
    if (source_tracks != target_tracks or
            abs(target_duration - source_duration) > min(2, max(0.5, source_duration * 0.001))):
        raise ValueError("无损封装的轨道、编码或时长未通过检查")


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


def finalize(source: Path, target: Path, ffmpeg: Path, ffprobe: Path, *, on_published=None) -> Path:
    source, target = checked_path(source), checked_path(target)
    formats = {".mkv": "matroska", ".mp4": "mp4"}
    if source == target or target.suffix.lower() not in formats:
        raise ValueError("无效的成品路径或容器")
    if target.exists():
        raise FileExistsError(target)
    staging = checked_path(target.with_name(f".liverecorder-{uuid4().hex}.partial"))
    owned = False
    try:
        staging.touch(exist_ok=False)
        owned = True
        command = [str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-y", "-i", str(source),
                   "-map", "0", "-c", "copy", "-f", formats[target.suffix.lower()]]
        if target.suffix.lower() == ".mp4":
            command.extend(["-movflags", "+faststart"])
        subprocess.run([*command, str(checked_path(staging))], capture_output=True, timeout=600, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        validate_remux(source, staging, ffprobe)
        stamp = file_identity(staging)
        if stamp is None:
            raise ValueError("无法校验封装成品身份")
        checked_path(target)
        if os.name == "nt":
            checked_path(staging).rename(target)  # Windows does not replace an existing destination.
        else:
            os.link(checked_path(staging), target)
        if on_published is not None:
            on_published(target, stamp)
        return target
    except FileExistsError:
        raise
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        raise RuntimeError("封装未完成，保留已录制的源文件") from error
    finally:
        if owned:
            checked_path(staging).unlink(missing_ok=True)
