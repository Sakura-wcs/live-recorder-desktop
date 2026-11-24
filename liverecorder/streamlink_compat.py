"""Keep the Windows FFmpeg launch compatibility shim scoped to Streamlink 8.6."""

import os
import subprocess


class _HiddenSubprocess:
    def __getattr__(self, attr):
        return getattr(subprocess, attr)

    @staticmethod
    def Popen(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(*args, **kwargs)


def _run_version_output(probe):
    # Streamlink's FFmpegVersionOutput delegates to Trio; the muxer subprocess
    # shim alone does not cover that separate executable launch.
    try:
        result = subprocess.run(
            probe.command, stdin=subprocess.DEVNULL, capture_output=True, timeout=probe.timeout,
            text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for index, line in enumerate(result.stdout.splitlines()):
        if probe.onstdout(index, line.strip()) is False:
            return False
    return probe.onexit(result.returncode)


def configure_hidden_ffmpeg():
    if os.name == "nt":
        from streamlink.stream import ffmpegmux

        if not isinstance(ffmpegmux.subprocess, _HiddenSubprocess):
            ffmpegmux.subprocess = _HiddenSubprocess()
        ffmpegmux.FFmpegVersionOutput.run = _run_version_output
