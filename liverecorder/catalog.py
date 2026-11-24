import json
import sqlite3
import stat
from time import time
from dataclasses import dataclass
from pathlib import Path

from .paths import checked_path
from .settings import PLATFORMS


@dataclass(frozen=True)
class Room:
    id: int
    platform: str
    identity: str
    name: str
    monitored: bool

    @property
    def display_name(self) -> str:
        return self.name or self.identity


@dataclass(frozen=True)
class Recording:
    id: int
    room_id: int
    path: Path
    original: tuple[int, int, int, int] | None = None
    published: bool = False

    def is_current(self) -> bool:
        return self.original is not None and self.original == file_identity(self.path)


def file_identity(path: Path):
    try:
        info = checked_path(path).stat()
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def directory_identity(path: Path):
    info = checked_path(path).lstat()
    if stat.S_ISDIR(info.st_mode) and info.st_ino:
        return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", 0)
    return None


class Catalog:
    def __init__(self, path: Path):
        path = checked_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(checked_path(path), timeout=8)
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                raise ValueError("录像库版本高于当前应用支持的版本")
            if version == 0:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE IF NOT EXISTS rooms (
                        id INTEGER PRIMARY KEY,
                        platform TEXT NOT NULL,
                        identity TEXT NOT NULL,
                        name TEXT NOT NULL,
                        monitored INTEGER NOT NULL CHECK(monitored IN (0, 1)),
                        UNIQUE(platform, identity)
                    );
                    CREATE TABLE IF NOT EXISTS recordings (
                        id INTEGER PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES rooms(id),
                        path TEXT NOT NULL UNIQUE
                    );
                    CREATE INDEX IF NOT EXISTS recordings_room ON recordings(room_id);
                    PRAGMA user_version = 1;
                    COMMIT;
                """)
            if version < 2:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE IF NOT EXISTS compression_batches (
                        id INTEGER PRIMARY KEY, codec TEXT NOT NULL, policy TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'running', created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS compression_events (
                        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES compression_batches(id),
                        data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS compression_event_batch ON compression_events(batch_id, id);
                    PRAGMA user_version = 2;
                    COMMIT;
                """)
            if version < 3:
                self.db.executescript("""
                    BEGIN;
                    ALTER TABLE recordings ADD COLUMN identity_stamp TEXT;
                    PRAGMA user_version = 3;
                    COMMIT;
                """)
            if version < 4:
                self.db.executescript("""
                    BEGIN;
                    ALTER TABLE rooms ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE recordings ADD COLUMN worker_published INTEGER NOT NULL DEFAULT 0;
                    CREATE TABLE room_directories (
                        path TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
                        room_id INTEGER NOT NULL REFERENCES rooms(id),
                        stamp TEXT NOT NULL
                    );
                    PRAGMA user_version = 4;
                    COMMIT;
                """)
            if version < 5:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE recording_sessions (
                        id INTEGER PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES rooms(id),
                        raw TEXT NOT NULL UNIQUE,
                        target TEXT NOT NULL UNIQUE,
                        raw_stamp TEXT NOT NULL,
                        output_stamp TEXT,
                        state TEXT NOT NULL DEFAULT 'capturing'
                    );
                    CREATE INDEX recording_sessions_state ON recording_sessions(state);
                    PRAGMA user_version = 5;
                    COMMIT;
                """)
            if version < 6:
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE platform_probe_slots (
                        platform TEXT PRIMARY KEY, next_at REAL NOT NULL
                    );
                    PRAGMA user_version = 6;
                    COMMIT;
                """)
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def save_room(self, platform: str, identity: str, name: str, *, monitored: bool | None = None) -> Room:
        identity, name = identity.strip(), name.strip()
        if platform not in PLATFORMS or not identity:
            raise ValueError("需要有效的平台和房间 ID")
        with self.db:
            result = self.db.execute("""
                INSERT INTO rooms(platform, identity, name, monitored) VALUES (?, ?, ?, coalesce(?, 0))
                ON CONFLICT(platform, identity) DO UPDATE
                    SET name=excluded.name, monitored=coalesce(?, rooms.monitored), hidden=0
                RETURNING id, platform, identity, name, monitored
            """, (platform, identity, name, monitored, monitored)).fetchone()
        return Room(*result[:4], bool(result[4]))

    def set_monitored(self, room_id: int, monitored: bool):
        with self.db:
            self.db.execute("UPDATE rooms SET monitored=? WHERE id=? AND hidden=0", (monitored, room_id))

    def fill_pending_name(self, room: Room, name: str) -> bool:
        if not name.strip():
            return False
        with self.db:
            return self.db.execute("""
                UPDATE rooms SET name=? WHERE id=? AND platform=? AND identity=?
                    AND hidden=0 AND monitored=1 AND name=''
            """, (name.strip(), room.id, room.platform, room.identity)).rowcount == 1

    def rooms(self, *, include_hidden=False) -> list[Room]:
        query = "SELECT id, platform, identity, name, monitored FROM rooms"
        rows = self.db.execute(query + ("" if include_hidden else " WHERE hidden=0") + " ORDER BY id")
        return [Room(*row[:4], bool(row[4])) for row in rows]

    def visible_room(self, room_id: int) -> Room | None:
        row = self.db.execute("SELECT id,platform,identity,name,monitored FROM rooms WHERE id=? AND hidden=0",
                              (room_id,)).fetchone()
        return Room(*row[:4], bool(row[4])) if row else None

    def archive(self, room_ids):
        with self.db:
            self.db.executemany("UPDATE rooms SET hidden=1, monitored=0 WHERE id=?", ((i,) for i in room_ids))

    def add_missing_rooms(self, entries):
        added = []
        with self.db:
            for platform, identity, name in entries:
                row = self.db.execute(
                    """INSERT INTO rooms(platform,identity,name,monitored) VALUES(?,?,?,0)
                    ON CONFLICT(platform,identity) DO UPDATE SET hidden=0,name=excluded.name,monitored=0
                    WHERE rooms.hidden=1 RETURNING id""", (platform, identity, name)
                ).fetchone()
                if row:
                    added.append((platform, identity))
        return added

    def edit_room(self, room_id, platform, identity, name):
        with self.db:
            old = self.db.execute("SELECT platform,identity FROM rooms WHERE id=? AND hidden=0", (room_id,)).fetchone()
            if old is None:
                raise ValueError("原房间已不在列表中")
            if old == (platform, identity):
                self.db.execute("UPDATE rooms SET name=? WHERE id=?", (name, room_id))
            else:
                conflict = self.db.execute(
                    "SELECT hidden FROM rooms WHERE platform=? AND identity=?", (platform, identity)
                ).fetchone()
                if conflict == (0,):
                    raise ValueError("目标房间已在列表中")
                self.db.execute("UPDATE rooms SET hidden=1,monitored=0 WHERE id=?", (room_id,))
                self.db.execute("""INSERT INTO rooms(platform,identity,name,monitored) VALUES(?,?,?,0)
                    ON CONFLICT(platform,identity) DO UPDATE SET hidden=0,monitored=0,name=excluded.name""",
                    (platform, identity, name))
        return next(room for room in self.rooms() if (room.platform, room.identity) == (platform, identity))

    def register_directory(self, room_id, path):
        path = checked_path(path)
        identity = directory_identity(path)
        if identity is None:
            raise ValueError("无法登记目录身份")
        with self.db:
            self.db.execute("INSERT INTO room_directories VALUES(?,?,?) ON CONFLICT(path) DO NOTHING",
                            (str(path), room_id, json.dumps(identity)))

    def owned_directories(self):
        return [(room_id, Path(path), tuple(json.loads(stamp))) for path, room_id, stamp in
                self.db.execute("SELECT path,room_id,stamp FROM room_directories")]

    def recording_directory(self, room_id: int, root: Path) -> tuple[Path, tuple] | None:
        for raw, stamp in self.db.execute("SELECT path,stamp FROM room_directories WHERE room_id=? ORDER BY rowid",
                                          (room_id,)):
            candidate = Path(raw)
            if candidate.parent != root:
                continue
            try:
                identity = directory_identity(candidate)
                if identity is not None and identity == tuple(json.loads(stamp)):
                    return candidate, identity
            except (OSError, TypeError, ValueError):
                continue
        return None

    def forget_directory(self, room_id, directory):
        with self.db:
            self.db.execute("DELETE FROM room_directories WHERE room_id=? AND path=?", (room_id, str(directory)))
            paths = [(item.id,) for item in self.recordings([room_id]) if item.path.is_relative_to(directory)]
            self.db.executemany("DELETE FROM recordings WHERE id=?", paths)

    def add_recording(self, room_id: int, path: Path, *, published_stamp=None) -> None:
        path = checked_path(path)
        identity = file_identity(path)
        if published_stamp is not None and published_stamp != identity:
            raise ValueError("已发布录像的身份发生变化")
        with self.db:
            self.db.execute(
                """INSERT INTO recordings(room_id, path, identity_stamp, worker_published)
                   VALUES (?, ?, ?, ?) ON CONFLICT(path) DO NOTHING""",
                (room_id, str(path), json.dumps(identity) if identity is not None else None, published_stamp is not None),
            )

    def recordings(self, room_ids: list[int] | None = None, *, limit=None, before=None) -> list[Recording]:
        sql = "SELECT id, room_id, path, identity_stamp, worker_published FROM recordings"
        parameters = ()
        if room_ids is not None:
            if not room_ids:
                return []
            sql += " WHERE room_id IN (SELECT value FROM json_each(?))"
            parameters = (json.dumps(room_ids),)
        if before is not None:
            sql += (" WHERE " if room_ids is None else " AND ") + "id < ?"
            parameters += (before,)
        sql += " ORDER BY id" if limit is None else " ORDER BY id DESC LIMIT ?"
        if limit is not None:
            parameters += (limit,)
        return [
            Recording(row[0], row[1], Path(row[2]), tuple(json.loads(row[3])) if row[3] else None, bool(row[4]))
            for row in self.db.execute(sql, parameters)
        ]

    def flag_interrupted_batches(self):
        with self.db:
            return self.db.execute(
                "UPDATE compression_batches SET state='interrupted' WHERE state='running'"
            ).rowcount

    def reserve_probe(self, platform, *, spacing=1.5):
        now = time()
        with self.db:
            reserved, = self.db.execute("""
                INSERT INTO platform_probe_slots VALUES (?, ?)
                ON CONFLICT(platform) DO UPDATE SET next_at =
                    min(max(platform_probe_slots.next_at, ?), ? + 60) + ?
                RETURNING next_at
                """, (platform, now + spacing, now, now, spacing)).fetchall()[0]
        return max(0.0, reserved - spacing - now)

    def begin_batch(self, codec: str, policy: str) -> int:
        with self.db:
            return self.db.execute(
                "INSERT INTO compression_batches(codec, policy) VALUES(?, ?) RETURNING id", (codec, policy)
            ).fetchall()[0][0]

    def append_batch_item(self, batch: int, item: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO compression_events(batch_id, data) VALUES(?, ?)",
                            (batch, json.dumps(item, ensure_ascii=False)))

    def finish_batch(self, batch: int, state: str) -> None:
        with self.db:
            self.db.execute("UPDATE compression_batches SET state=? WHERE id=?", (state, batch))

    def batch_history(self, batch: int) -> tuple[str, list[dict]]:
        state, = self.db.execute("SELECT state FROM compression_batches WHERE id=?", (batch,)).fetchone()
        rows = self.db.execute("SELECT data FROM compression_events WHERE batch_id=? ORDER BY id", (batch,))
        return state, [json.loads(data) for (data,) in rows]
