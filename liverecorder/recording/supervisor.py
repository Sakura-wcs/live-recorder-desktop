import json
import os
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, SimpleQueue

from .protocol import RoomEvent


def _process_job(process):
    if os.name != "nt":
        return None
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE | win32con.PROCESS_SET_QUOTA, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except BaseException:
        job.Close()
        raise


class Supervisor:
    def __init__(self, command, *, limit=8, stop_timeout=35):
        self._command = command
        self._limit = limit
        self._stop_timeout = stop_timeout
        self._active = {}
        self._deadlines = {}
        self._phases = {}
        self._events = SimpleQueue()

    def start(self, room_id: int) -> bool:
        self._reap()
        if room_id in self._active or len(self._active) >= self._limit:
            return False
        process = subprocess.Popen(
            self._command(room_id), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            job = _process_job(process)
        except Exception:
            process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
            raise OSError("无法为工作进程建立 Windows 进程组") from None
        reader = threading.Thread(target=self._read, args=(room_id, process), daemon=True)
        self._active[room_id] = process, reader, job
        reader.start()
        return True

    def set_limit(self, limit: int) -> None:
        if not 1 <= limit <= 32:
            raise ValueError("并发上限应在 1 到 32 之间")
        self._limit = limit

    def _read(self, room_id, process):
        for line in process.stdout:
            try:
                data = json.loads(line)
                if data["room_id"] != room_id:
                    continue
                path = Path(data["path"]) if data.get("path") else None
                self._events.put(RoomEvent(room_id, data["state"], data["message"], path))
            except (ValueError, KeyError, TypeError):
                continue

    def active(self, room_id: int) -> bool:
        return room_id in self._active and self._active[room_id][0].poll() is None

    def pending(self, room_id=None) -> bool:
        return bool(self._active) if room_id is None else room_id in self._active

    def _timeout(self, room_id):
        return 1260 if self._phases.get(room_id) == "finalizing" else self._stop_timeout

    def stop(self, room_id: int) -> None:
        if not self.active(room_id) or room_id in self._deadlines:
            return
        self._deadlines[room_id] = time.monotonic() + self._timeout(room_id)
        try:
            process = self._active[room_id][0]
            process.stdin.write("stop\n")
            process.stdin.flush()
        except OSError:
            pass

    def _reap(self):
        for room_id, (process, reader, job) in list(self._active.items()):
            deadline = self._deadlines.get(room_id)
            if deadline is not None and time.monotonic() >= deadline and process.poll() is None:
                if job is not None:
                    job.Close()
                    self._active[room_id] = process, reader, None
                    job = None
                else:
                    process.kill()
                self._deadlines.pop(room_id, None)
            if process.poll() is None:
                continue
            if job is not None:
                job.Close()
                self._active[room_id] = process, reader, None
            if reader.is_alive():
                continue
            process.stdin.close()
            process.stdout.close()
            self._active.pop(room_id)
            self._deadlines.pop(room_id, None)
            self._phases.pop(room_id, None)
            state = "stopped" if process.returncode == 0 else "failed"
            message = "监控已停止" if state == "stopped" else "工作进程异常结束，可检查未完成文件"
            self._events.put(RoomEvent(room_id, state, message))

    def events(self) -> list[RoomEvent]:
        events = []

        def drain():
            while True:
                try:
                    event = self._events.get_nowait()
                except Empty:
                    return
                past = self._phases.get(event.room_id)
                if event.room_id in self._active:
                    self._phases[event.room_id] = event.state
                if event.room_id in self._deadlines and past != event.state and "finalizing" in (past, event.state):
                    self._deadlines[event.room_id] = time.monotonic() + self._timeout(event.room_id)
                events.append(event)

        drain()
        self._reap()
        drain()
        return events

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for room_id in self._active:
            self.stop(room_id)
        while self._active:
            self.events()
            if self._active:
                time.sleep(0.025)
