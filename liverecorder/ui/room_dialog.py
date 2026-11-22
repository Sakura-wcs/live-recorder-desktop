from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from ..platforms import NAMES, normalize_room

_LOOKUPS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="room-name")


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
        self.toast = QLabel("自动获取名称失败，请手动填写")
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
        self.alias.setPlaceholderText("尝试自动填写，也可手动修改")
        if room is not None:
            self.setWindowTitle("编辑直播间")
            self.identifier.setText(room.identity)
            self.alias.setText(room.name)
            self.manual = True
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
        self.deadline.timeout.connect(self._failure)
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

    def _manual_changed(self, text):
        self.name_edited = True
        self.manual = bool(text.strip())
        if self.manual:
            self.toast.hide()
            self.note.clear()
        elif self.last_requested is None and self.identifier.text().strip():
            self.debounce.start()

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
        if not self.future.done():
            return
        result = self.future
        self._cancel()
        self.note.clear()
        if self.manual:
            return
        try:
            name = result.result()
        except Exception:
            name = None
        if isinstance(name, str) and name.strip():
            self.alias.setText(name.strip())
        else:
            self._failure()

    def _failure(self):
        self._cancel()
        self.note.clear()
        if not self.manual:
            self.toast.show()
            self.hide_toast.start()

    def _save(self):
        try:
            self.identity = self._normalize()
            if self.validate is not None:
                self.validate(*self.identity, self.alias.text().strip() or self.identity[1])
        except ValueError as error:
            self.note.setText(str(error))
            return
        self.accept()
