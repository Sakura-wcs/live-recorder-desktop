"""Coordinate persisted monitoring intent with the room worker lifecycle."""

from .supervisor import Supervisor


class RecordingService:
    def __init__(self, catalog, command, limit):
        self.catalog = catalog
        self.supervisor = Supervisor(command, limit=limit)

    def start(self, room):
        current = self.catalog.visible_room(room.id)
        if current is None or self.supervisor.pending(room.id):
            return False
        if not current.monitored:
            self.catalog.set_monitored(room.id, True)
        started = False
        try:
            started = self.supervisor.start(room.id)
            return started
        finally:
            if not started and not current.monitored:
                self.catalog.set_monitored(room.id, False)

    def stop(self, room):
        self.supervisor.stop(room.id)
        self.catalog.set_monitored(room.id, False)

    def resume(self):
        for room in self.catalog.rooms():
            if room.monitored and self.supervisor.start(room.id):
                yield room
