import argparse
import logging
import sqlite3
from hashlib import sha256
from os.path import normcase
from pathlib import Path

from ..catalog import Catalog
from ..paths import AppPaths
from ..platforms import adapter_for
from ..settings import PlatformSettings, WindowsCredentials
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
    paths = AppPaths.from_root(args.data)
    with Catalog(paths.catalog) as catalog:
        room = next(item for item in catalog.rooms() if item.id == args.room_id)
    adapter = adapter_for(room.platform)
    root = paths.recording_root(args.recordings)
    namespace = sha256(normcase(str(paths.catalog.resolve())).encode("utf-8")).hexdigest()[:16]
    platform_settings = PlatformSettings(paths.platforms, WindowsCredentials())

    def capture(stop, emit):
        def open_stream():
            return adapter.detect(room.identity, platform_settings.load(room.platform), args.ffmpeg)

        def register(directory):
            try:
                with Catalog(paths.catalog) as catalog:
                    catalog.register_directory(room.id, directory)
            except (sqlite3.Error, OSError, ValueError):
                emit("notice", "目录归属登记失败。录像仍会保存，该目录不会被自动永久删除。")

        def register_publication(path, stamp):
            try:
                with Catalog(paths.catalog) as catalog:
                    catalog.add_recording(room.id, path, published_stamp=stamp)
            except (sqlite3.Error, OSError, ValueError):
                emit("notice", "未能核验录像的发布记录。已保留成品，不允许自动永久删除。")

        record_loop(stop, emit, open_stream, root, room.name, room.platform, room.id,
                    args.ffmpeg, args.ffprobe, container="." + args.container, on_directory_created=register,
                    on_published=register_publication, namespace=namespace)

    serve(room.id, capture)


if __name__ == "__main__":
    main()
