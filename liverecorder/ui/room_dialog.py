from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from ..platforms import NAMES, NameLookup, normalize_room

_LOOKUPS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="room-name")
_FAILED = "自动获取名称失败，请手动填写"


class RoomDialog(QDialog):
    def __init__(self, lookup, parent=None, *, platform=None, room=None, validate=None):
        super().__init__(parent)
        self.lookup = lookup
        self.validate = validate
        self.original_room = room
        self.identity = None
        self.manual = False
        self.name_edited = False
        self.future = None
        self.last_requested = None
        self.setWindowTitle("保存直播间")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.toast = QLabel(_FAILED)
        self.toast.setStyleSheet("background:#433627;color:#ffe3b2;padding:8px;border-radius:5px;")
        self.toast.setWordWrap(True)
        self.toast.hide()
        layout.addWidget(self.toast)
        form = QFormLayout()
        layout.addLayout(form)
        self.platform = QComboBox()
        for key, name in NAMES.items():
            self.platform.addItem(name, key)
        default = self.platform.findData(room.platform if room else platform)
        if default >= 0:
            self.platform.setCurrentIndex(default)
        self.identifier = QLineEdit()
        self.identifier.setPlaceholderText("粘贴直播链接或填写房间 ID")
        self.identifier.setToolTip("平台和房间 ID 共同确定房间；更换房间不会搬动原录像。")
        self.alias = QLineEdit()
        self.alias.setMaxLength(128)
        self.alias.setPlaceholderText("可留空，开始监控后自动补齐")
        if room is not None:
            self.setWindowTitle("编辑直播间")
            self.identifier.setText(room.identity)
            self.alias.setText(room.name)
            self.manual = bool(room.name)
        self.note = QLabel()
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        form.addRow("平台", self.platform)
        form.addRow("房间 ID 或链接", self.identifier)
        form.addRow("主播名称", self.alias)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        self.debounce = QTimer(self, singleShot=True, interval=450)
        self.poll = QTimer(self, interval=60)
        self.hide_toast = QTimer(self, singleShot=True, interval=3200)
        self.deadline = QTimer(self, singleShot=True, interval=9000)
        self.debounce.timeout.connect(self._request)
        self.poll.timeout.connect(self._collect)
        self.hide_toast.timeout.connect(self.toast.hide)
        self.deadline.timeout.connect(lambda: self._failure(NameLookup(reason="timeout").notice))
        self.platform.currentIndexChanged.connect(self._changed)
        self.identifier.textChanged.connect(self._changed)
        self.alias.textEdited.connect(self._manual_changed)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.finished.connect(self._cancel)

    def _cancel(self):
        self.debounce.stop()
        self.poll.stop()
        self.deadline.stop()
        if self.future is not None:
            self.future.cancel()
            self.future = None

    def _changed(self):
        self._cancel()
        self.toast.hide()
        self.note.clear()
        self.last_requested = None
        if self.name_edited and not self.alias.text().strip():
            self.name_edited = self.manual = False
        if self.original_room is not None and not self.name_edited:
            try:
                same = normalize_room(self.platform.currentData(), self.identifier.text()) == self.original_room.identity
            except ValueError:
                same = False
            self.manual = same and self.platform.currentData() == self.original_room.platform
            self.alias.setText(self.original_room.name if self.manual else "")
        elif not self.manual:
            self.alias.clear()
        if self.identifier.text().strip():
            self.debounce.start()

    def _manual_changed(self, _text):
        self.name_edited = True
        self.manual = True  # Explicitly clearing a name is also an edit.
        self._cancel()
        self.toast.hide()
        self.note.clear()

    def _normalize(self):
        platform = self.platform.currentData()
        identity = normalize_room(platform, self.identifier.text())
        if platform == "douyin" and identity != self.identifier.text():
            self.identifier.blockSignals(True)
            self.identifier.setText(identity)
            self.identifier.blockSignals(False)
        return platform, identity

    def _request(self):
        try:
            key = self._normalize()
        except ValueError:
            return
        if self.manual or key == self.last_requested:
            return
        self.last_requested = key
        self.note.setText("正在获取主播名称…")
        self.future = _LOOKUPS.submit(self.lookup, *key)
        self.poll.start()
        self.deadline.start()

    def _collect(self):
        if self.future is None or not self.future.done():
            return
        result = self.future
        self._cancel()
        self.note.clear()
        if self.manual:
            return
        try:
            lookup = result.result()
        except Exception:
            lookup = NameLookup(reason="platform")
        name = lookup.name if isinstance(lookup, NameLookup) else lookup
        if isinstance(name, str) and name.strip():
            self.alias.setText(name.strip())
        else:
            self._failure(lookup.notice if isinstance(lookup, NameLookup) else _FAILED)

    def _failure(self, message):
        self._cancel()
        self.note.clear()
        if not self.manual:
            self.toast.setText(message)
            self.toast.show()
            self.hide_toast.start()

    def _save(self):
        try:
            self.identity = self._normalize()
            if self.validate is not None:
                self.validate(*self.identity, self.alias.text().strip())
        except ValueError as error:
            self.note.setText(str(error))
            return
        self.accept()
