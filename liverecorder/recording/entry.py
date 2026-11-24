import argparse
import logging
import os
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from os.path import normcase
from pathlib import Path

from ..catalog import Catalog
from ..paths import AppPaths, require_recording_volume
from ..platforms import adapter_for
from ..settings import PlatformSettings, WindowsCredentials
from ..streamlink_compat import configure_hidden_ffmpeg
from .recovery import RecordingJournal
from .worker import record_loop, serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("room_id", type=int)
    parser.add_argument("data", type=Path)
    parser.add_argument("recordings", type=Path)
    parser.add_argument("ffmpeg", type=Path)
    parser.add_argument("ffprobe", type=Path)
    parser.add_argument("--container", choices=["mkv", "mp4"], default="mkv")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    configure_hidden_ffmpeg()
    paths = AppPaths.from_root(args.data)
    with Catalog(paths.catalog) as catalog:
        room = next(item for item in catalog.rooms() if item.id == args.room_id)
    adapter = adapter_for(room.platform)
    root = require_recording_volume(paths.recording_root(args.recordings))
    namespace = sha256(normcase(str(paths.catalog.resolve())).encode("utf-8")).hexdigest()[:16]
    platform_settings = PlatformSettings(paths.platforms, WindowsCredentials())

    def capture(stop, emit):
        root.mkdir(parents=True, exist_ok=True)
        require_recording_volume(root)
        os.environ["TEMP"] = os.environ["TMP"] = str(root)
        def open_stream():
            with Catalog(paths.catalog) as catalog:
                wait = catalog.reserve_probe(room.platform)
            if stop.wait(wait):
                return None
            return adapter.detect(room.identity, platform_settings.load(room.platform), args.ffmpeg)

        def register(directory):
            try:
                with Catalog(paths.catalog) as catalog:
                    catalog.register_directory(room.id, directory)
            except (sqlite3.Error, OSError, ValueError):
                emit("notice", "目录归属登记失败。录像仍会保存，该目录不会被自动永久删除。")

        @contextmanager
        def journal():
            with Catalog(paths.catalog) as catalog:
                yield RecordingJournal(catalog)

        record_loop(stop, emit, open_stream, root, room.name, room.platform, room.id,
                    args.ffmpeg, args.ffprobe, container="." + args.container, on_directory_created=register,
                    journal=journal, namespace=namespace)

    serve(room.id, capture)


if __name__ == "__main__":
    main()
