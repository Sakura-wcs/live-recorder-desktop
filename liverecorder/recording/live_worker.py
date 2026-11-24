"""Single-file live capture with independently bounded publication."""

import os
import random
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from uuid import uuid4

from ..catalog import directory_identity, file_identity
from ..paths import require_recording_handle, require_recording_volume, safe_component, safe_join
from .live_media import LiveMkv, quick_check, require_live_mkv_tool
from .media import RemuxError
from .recovery import source_key
from .worker import OutputSpaceError, output_directory


_BLOCK = 256 * 1024
_OVERLAP = 32 * 1024
_RECONNECT_BYTES = 4 * 1024 * 1024
_RECONNECT_SECONDS = 3
_ERRORS = (OSError, RuntimeError, ValueError, sqlite3.Error)


def resume_bytes(tail, prefix):
    """Only append a new connection when it provides a unique byte-for-byte overlap."""
    if len(tail) < 4096:
        return None
    at = prefix.find(tail)
    if at < 0 or prefix.find(tail, at + 1) >= 0:
        return None
    return prefix[at + len(tail):]


def _resume_stream(tail, head, stream, stop):
    prefix = bytearray(head)
    deadline = monotonic() + _RECONNECT_SECONDS
    while not stop.is_set():
        match = resume_bytes(tail, prefix)
        if match is not None:
            return bytes(match)
        if len(prefix) >= _RECONNECT_BYTES or monotonic() >= deadline:
            break
        block = stream.read(min(_BLOCK, _RECONNECT_BYTES - len(prefix)))
        if not block:
            break
        prefix.extend(block)
    return None


@dataclass
class _LiveClip:
    writer: LiveMkv
    partial: object
    target: object
    guard: object
    key: tuple
    session: int | None
    extension: str
    tail: bytes = b""
    next_check: float = 0


def record_live_loop(stop, emit, open_stream, root, name, platform, room_id, ffmpeg, ffprobe,
                     *, interval=15, confirm_interval=5, offline_confirmations=3, min_free_bytes=512 * 1024**2,
                     on_directory_created=None, namespace="", journal=None, resolve_output=None):
    clip = None
    blocked = False
    misses = 0
    delay = interval
    settling = []

    def mark_failed(current, detail):
        if journal and current.session is not None:
            try:
                with journal() as log:
                    log.mark_seal_failed(current.session)
            except _ERRORS:
                pass  # The initial journal entry still supports interrupted-capture discovery.
        emit("live_seal_failed", detail, current.partial)

    def close_and_publish(current, issue):
        try:
            current.writer.finish()
            if issue:
                mark_failed(current, issue)
                return
            partial = current.guard(current.partial)
            if source_key(partial) != current.key:
                raise ValueError("临时录像身份已变化")
            stamp = file_identity(partial)
            quick_check(partial, ffprobe)
            if stamp is None or file_identity(current.guard(partial)) != stamp:
                raise ValueError("临时录像在检查期间变化")
            if journal:
                with journal() as log:
                    log.prepare(current.session, stamp)
            target = current.guard(current.target)
            if target.exists():
                raise FileExistsError(target)
            if os.name == "nt":
                current.guard(partial).rename(target)
            else:
                os.link(current.guard(partial), target)
                current.guard(partial).unlink()
            if journal:
                with journal() as log:
                    log.complete(current.session, room_id, target, stamp)
            emit("live_published", "录制成品已写入", target)
        except _ERRORS as error:
            current.writer.abort()
            failure = str(error) if isinstance(error, RemuxError) else "收尾或登记未完成，已保留待恢复录像"
            mark_failed(current, f"{issue}；{failure}" if issue else failure)

    def start_clip(extension, title):
        if extension not in {".flv", ".ts", ".mkv", ".mp4"}:
            raise RemuxError("直播容器不支持实时封装")
        require_live_mkv_tool(ffmpeg)
        display_name, preferred = resolve_output() if resolve_output else (name, None)
        directory = output_directory(root, display_name, platform, room_id, min_free_bytes=min_free_bytes,
                                     on_create=on_directory_created, namespace=namespace, preferred=preferred,
                                     require_owned=resolve_output is not None)
        stamp = preferred[1] if preferred is not None and preferred[0] == directory else directory_identity(directory)
        if stamp is None:
            raise ValueError("无法确认主播录像目录身份")

        def guard(path):
            path = require_recording_volume(path)
            if directory_identity(directory) != stamp:
                raise ValueError("主播录像目录身份已变化")
            return path

        stem = f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{uuid4().hex[:10]}_{safe_component(title)[:40]}"
        partial = guard(safe_join(directory, stem + ".live.partial.mkv"))
        target = safe_join(directory, stem + ".mkv")
        with partial.open("xb") as reserved:
            require_recording_handle(reserved, partial)
        key = source_key(partial)
        if key is None:
            raise ValueError("无法确认临时录像身份")
        session = None
        if journal:
            with journal() as log:
                session = log.begin(room_id, partial, target)
        try:
            writer = LiveMkv(partial, extension, ffmpeg, guard=guard)
        except _ERRORS:
            if journal and session is not None:
                try:
                    with journal() as log:
                        log.mark_seal_failed(session)
                except _ERRORS:
                    pass
            raise
        return _LiveClip(writer, partial, target, guard, key, session, extension)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-mkv-seal") as pool:
        def submit(current, issue=None):
            if current is None:
                return
            settling[:] = [future for future in settling if not future.done()]
            if len(settling) >= 2:
                # An abnormal burst must not keep an unbounded number of FFmpeg children alive.
                current.writer.abort()
                mark_failed(current, "收尾任务已满，已保留当前待恢复录像")
                return
            emit("live_sealing", "正在快速检查并收尾", current.partial)
            settling.append(pool.submit(close_and_publish, current, issue))

        try:
            while not stop.is_set():
                try:
                    require_recording_volume(root)
                    emit("live_confirm" if (clip or blocked) else "checking",
                         "正在确认直播是否结束或能否续录" if (clip or blocked) else "正在检测开播")
                    opened = open_stream()
                    if opened is None:
                        if stop.is_set():
                            break
                        if clip or blocked:
                            misses += 1
                            if misses < offline_confirmations:
                                stop.wait(confirm_interval)
                                continue
                            submit(clip)
                            clip, blocked, misses = None, False, 0
                        emit("offline", "未发现可用直播流，等待下次检测")
                        delay = interval
                        stop.wait(interval * random.uniform(1, 1.3))
                        continue
                    stream, extension, title = opened
                    misses = 0
                    if blocked:
                        with stream:
                            pass
                        stop.wait(interval)
                        continue
                    with stream:
                        try:
                            head = stream.read(_BLOCK)
                        except OSError:
                            stop.wait(confirm_interval)
                            continue
                        if not head or stop.is_set():
                            stop.wait(confirm_interval)
                            continue
                        if clip:
                            try:
                                start = _resume_stream(clip.tail, head, stream, stop) if extension == clip.extension else None
                            except OSError:
                                stop.wait(confirm_interval)
                                continue
                            if stop.is_set():
                                break
                            if start is None:
                                submit(clip, "重连后的直播流无法安全续写，已保留这一场的待恢复录像")
                                clip, blocked = None, True
                                stop.wait(interval)
                                continue
                            head = start
                        else:
                            try:
                                clip = start_clip(extension, title)
                            except _ERRORS:
                                blocked = True
                                raise
                        emit("recording", "正在实时无损封装", clip.partial)
                        block = head
                        while not stop.is_set():
                            if block:
                                now = monotonic()
                                if now >= clip.next_check:
                                    if shutil.disk_usage(clip.guard(clip.partial).parent).free < min_free_bytes + len(block):
                                        raise OutputSpaceError("录像盘空间不足，已保留当前待恢复录像")
                                    clip.next_check = now + 2
                                clip.writer.write(block)
                                clip.tail = (clip.tail + block)[-_OVERLAP:]
                            try:
                                block = stream.read(_BLOCK)
                            except OSError:
                                break
                            if not block:
                                break
                    delay = interval
                    if not stop.is_set():
                        stop.wait(confirm_interval)
                except _ERRORS as error:
                    misses = 0  # Failed probes never count as evidence of an ended broadcast.
                    # Adapter errors are distinct from local output failures.
                    code = getattr(error, "code", None)
                    if code:
                        emit(code, str(error))
                    elif clip:
                        soon = str(error) if isinstance(error, (RemuxError, OutputSpaceError)) else "录制中断，已保留待恢复录像"
                        submit(clip, soon)
                        clip, blocked = None, True
                    else:
                        emit("retrying", str(error) if isinstance(error, (RemuxError, OutputSpaceError)) else
                             "录制未完成，请检查录像盘与媒体工具")
                    if stop.wait(delay * random.uniform(1, 1.3)):
                        break
                    delay = min(max(interval, delay * 2), 300)
        finally:
            submit(clip)
