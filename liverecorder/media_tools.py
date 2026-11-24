"""Resolve and validate the media executables shared by recording and compression."""

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_LABELS = {"ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}
_VIDEO_ENCODER = re.compile(r"^\s*V[.A-Z]{5}\s+(\S+)\s+", re.MULTILINE)


def _find_tool(name: str, configured: str | Path | None) -> Path:
    label = _LABELS[name]
    if isinstance(configured, str):
        configured = configured.strip()
    suffix = ".exe" if os.name == "nt" else ""
    if configured:
        candidates = (Path(configured),)
    else:
        # Search only absolute PATH entries. On Windows, bare executable lookup
        # can otherwise prioritize a binary from the working directory.
        path_dirs = (Path(entry.strip('"')) for entry in os.get_exec_path())
        candidates = (directory / (name + suffix) for directory in path_dirs if directory.is_absolute())
    for path in candidates:
        if not path.is_absolute() or (suffix and path.suffix.lower() != suffix):
            break
        try:
            if not path.is_file() or not shutil.which(str(path)):
                continue
            resolved = path.resolve(strict=True)
            if not suffix or resolved.suffix.lower() == suffix:
                return resolved
        except (OSError, RuntimeError, ValueError):
            continue
    if configured:
        raise ValueError(f"已指定的 {label} 路径无效，请选择可执行文件的绝对路径。")
    raise ValueError(f"未找到 {label}，请在媒体工具设置中指定路径，或将其加入系统 PATH。")


def resolve_tools(preferences: Mapping[str, str | Path] | None = None) -> tuple[Path, Path]:
    preferences = preferences or {}
    return (_find_tool("ffmpeg", preferences.get("ffmpeg_path")),
            _find_tool("ffprobe", preferences.get("ffprobe_path")))


@dataclass(frozen=True)
class ToolDiagnostics:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_version: str
    ffprobe_version: str
    encoders: frozenset[str]


def _query(path: Path, name: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            [str(path), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", check=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"无法运行或查询 {_LABELS[name]}，请检查路径和可执行文件。") from None
    return completed.stdout


def _version(path: Path, name: str) -> str:
    output = _query(path, name, "-version")
    found = re.search(rf"^{name} version (\S+)", output, flags=re.MULTILINE)
    if not found:
        raise ValueError(f"选定的文件不是可识别的 {_LABELS[name]}。")
    return found.group(1)


def diagnose_tools(preferences: Mapping[str, str | Path] | None = None) -> ToolDiagnostics:
    ffmpeg, ffprobe = resolve_tools(preferences)
    ffmpeg_version = _version(ffmpeg, "ffmpeg")
    ffprobe_version = _version(ffprobe, "ffprobe")
    encoders = frozenset(_VIDEO_ENCODER.findall(_query(ffmpeg, "ffmpeg", "-hide_banner", "-encoders")))
    return ToolDiagnostics(ffmpeg, ffprobe, ffmpeg_version, ffprobe_version, encoders)
