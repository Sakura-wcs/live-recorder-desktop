import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from time import monotonic
from uuid import uuid4

from ..paths import checked_path, safe_component, safe_join
from .scan import OUTPUT_FOLDER, Candidate, ProbeCancelled, Snapshot, inspect, snapshot

ENCODERS = {("hevc", "gpu"): "hevc_nvenc", ("av1", "gpu"): "av1_nvenc",
            ("hevc", "cpu"): "libx265", ("av1", "cpu"): "libsvtav1"}
QUALITY = {"quality": (19, 22, 23), "balanced": (23, 26, 29), "small": (28, 30, 35)}


@dataclass(frozen=True)
class Profile:
    codec: str
    device: str
    preset: str
    quality: int | None = None

    def effective_quality(self):
        ratings = QUALITY[self.preset]
        quality = ratings[0 if self.device == "gpu" else 1 if self.codec == "hevc" else 2]
        if self.quality is not None:
            if not 1 <= self.quality <= (63 if (self.codec, self.device) == ("av1", "cpu") else 51):
                raise ValueError("质量参数超出编码器允许范围")
            quality = self.quality
        return quality

    def args(self):
        enc = ENCODERS[self.codec, self.device]
        quality = self.effective_quality()
        if self.device == "gpu":
            return ["-c:v:0", enc, "-preset:v:0", "p5", "-rc:v:0", "vbr", "-cq:v:0", str(quality), "-b:v:0", "0"]
        return ["-c:v:0", enc, "-preset:v:0", "medium" if self.codec == "hevc" else "8", "-crf:v:0", str(quality)]


@dataclass(frozen=True)
class Result:
    candidate: Candidate
    output: Path | None
    output_snapshot: Snapshot | None
    state: str
    message: str = ""


def _run(executable, args, cancel, *, progress=None, duration=0.0, stall_timeout=180.0):
    if cancel.is_set():
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    options = ["-progress", "pipe:1", "-nostats"]
    with subprocess.Popen(
        [str(executable), "-nostdin", "-v", "error", *options, *args],
        stdout=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=flags,
    ) as process:
        last_progress = monotonic()

        def read_progress():
            nonlocal last_progress
            values = {}
            best = {name: -1 for name in ("frame", "out_time_us", "total_size")}
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                values[key] = value
                if key != "progress":
                    continue
                for name, previous in best.items():
                    try:
                        observed = int(values[name])
                    except (ValueError, KeyError):
                        continue
                    if observed > previous:
                        best[name] = observed
                        last_progress = monotonic()
                if progress is not None and best["out_time_us"] >= 0:
                    seconds = best["out_time_us"] / 1_000_000
                    done = f"{min(100, seconds / duration * 100):.0f}%" if duration > 0 else f"{seconds:.0f} 秒"
                    progress(f"{done} · {values.get('speed', '—').strip()}")
                values.clear()

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        stalled = False
        while process.poll() is None:
            if cancel.wait(0.2):
                process.kill()
                break
            if process.poll() is None and monotonic() - last_progress >= stall_timeout:
                stalled = True
                process.kill()
                break
        process.wait()
        reader.join()
        if cancel.is_set():
            return False
        if stalled:
            raise RuntimeError("FFmpeg 无进度超时，已停止处理；原片保留")
        if process.returncode:
            raise RuntimeError("FFmpeg 处理失败：请检查编码器、源文件和剩余空间")
        return True


def preflight(profile, ffmpeg, cancel):
    return _run(ffmpeg, ["-f", "lavfi", "-i", "testsrc2=s=160x90:r=10",
                         "-frames:v", "2", *profile.args(), "-f", "null", "-"], cancel, stall_timeout=30)


def _verify(source, output, codec):
    streams = output["streams"]
    original = source["streams"]
    # We explicitly exclude cover art from supported inputs so stream indices remain predictable.
    if len(streams) != len(original):
        raise ValueError("成品轨道数不同")
    for i, (before, after) in enumerate(zip(original, streams, strict=True)):
        if before["codec_type"] != after["codec_type"] or after["codec_name"] != (codec if i == 0 else before["codec_name"]):
            raise ValueError("成品轨道规格发生变化")
        count_key = "nb_read_frames" if before["codec_type"] == "video" else "nb_read_packets"
        if not str(before.get(count_key, "")).isdigit() or before.get(count_key) != after.get(count_key):
            raise ValueError("无法证明每个轨道保留完整")
    before, after = original[0], streams[0]
    for key in ("width", "height", "color_space", "color_transfer", "color_primaries", "color_range", "pix_fmt"):
        if before.get(key) not in {None, "unknown", "unspecified"} and before.get(key) != after.get(key):
            raise ValueError("成品分辨率或色彩信息发生变化")
    for item in (before, after):
        if item.get("side_data_list"):
            raise ValueError("此版本不能验证源片的旋转或 HDR 附加数据")
    src_rate = Fraction(before.get("r_frame_rate", "0/1"))
    if src_rate > 0 and abs(Fraction(after.get("r_frame_rate", "0/1")) - src_rate) > Fraction(1, 20):
        raise ValueError("成品帧率与原片不符")
    src_duration = float(source["format"]["duration"])
    out_duration = float(output["format"]["duration"])
    if abs(src_duration - out_duration) > max(0.5, src_duration * 0.001):
        raise ValueError("成品时长与原片不符")


def encode(candidate, profile, ffmpeg, ffprobe, cancel, *, destination=None, progress=None):
    staging = None
    owned = False
    published = None
    try:
        if cancel.is_set():
            return Result(candidate, None, None, "cancelled")
        if (candidate.state != "ready" or candidate.classify(profile.codec).state != "ready"
                or snapshot(candidate.path) != candidate.snapshot):
            raise ValueError("源文件已变化或无需重新编码")
        source = candidate.media
        if source["streams"][0] != candidate.video or any(
            stream.get("disposition", {}).get("attached_pic") for stream in source["streams"]
        ):
            raise ValueError("视频轨顺序或封面轨需要手动处理")
        if candidate.video.get("side_data_list"):
            raise ValueError("此版本不能验证源片的旋转或 HDR 附加数据")
        directory = checked_path(destination) if destination else safe_join(candidate.path.parent, OUTPUT_FOLDER)
        directory.mkdir(parents=True, exist_ok=True)
        checked_path(directory)
        if shutil.disk_usage(directory).free < candidate.snapshot.size + 128 * 1024**2:
            raise ValueError("输出盘空间不足，源文件未修改")
        stem = safe_component(candidate.path.stem)[:65]
        staging = safe_join(directory, f".liverecorder-{uuid4().hex}.partial")
        staging.touch(exist_ok=False)
        owned = True
        args = ["-y", "-xerror", "-i", str(candidate.path), "-map", "0", "-map_metadata", "0", "-c", "copy",
                *profile.args(), "-f", "matroska", str(staging)]
        duration = float(source["format"].get("duration", 0))
        if not _run(ffmpeg, args, cancel, progress=progress, duration=duration):
            return Result(candidate, None, None, "cancelled")
        if progress:
            progress("正在校验成品")
        _verify(inspect(candidate.path, ffprobe, count_frames=True, cancel=cancel),
                inspect(staging, ffprobe, count_frames=True, cancel=cancel), profile.codec)
        if not _run(ffmpeg, ["-xerror", "-i", str(staging), "-map", "0:v", "-map", "0:a?",
                             "-f", "null", "-"], cancel):
            return Result(candidate, None, None, "cancelled")
        if snapshot(candidate.path) != candidate.snapshot:
            raise ValueError("源文件在编码时发生变化")
        if cancel.is_set():
            return Result(candidate, None, None, "cancelled")
        out = safe_join(directory, f"{stem}.{profile.codec}.mkv")
        if out.exists():
            out = safe_join(directory, f"{stem}.{profile.codec}.{uuid4().hex[:10]}.mkv")
        checked_path(staging)
        if os.name == "nt":
            staging.rename(out)
        else:
            os.link(staging, out)
        published = out
        info = snapshot(out)
        return Result(candidate, out, info, "success" if info.size < candidate.snapshot.size else "no_savings")
    except ProbeCancelled:
        return Result(candidate, None, None, "cancelled")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError, ArithmeticError) as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "读取、写入或校验视频失败"
        return Result(candidate, published, None, "failed", message)
    finally:
        if owned:
            checked_path(staging).unlink(missing_ok=True)
