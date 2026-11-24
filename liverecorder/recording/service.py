"""Coordinate persisted monitoring intent with the room worker lifecycle."""

from .supervisor import Supervisor


class RecordingService:
    def __init__(self, catalog, command, limit):
        self.catalog = catalog
        self.supervisor = Supervisor(command, limit=limit)

    def start(self, room):
        started = self.supervisor.start(room.id)
        if started:
            try:
                self.catalog.save_room(room.platform, room.identity, room.name, monitored=True)
            except Exception:
                self.supervisor.stop(room.id)
                raise
        return started

    def stop(self, room):
        self.supervisor.stop(room.id)
        self.catalog.save_room(room.platform, room.identity, room.name, monitored=False)

    def resume(self):
        for room in self.catalog.rooms():
            if room.monitored and self.supervisor.start(room.id):
                yield room
