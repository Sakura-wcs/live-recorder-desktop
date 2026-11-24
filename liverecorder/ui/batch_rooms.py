from collections import deque
from time import monotonic

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHeaderView, QLabel, QPlainTextEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..platforms import NAMES, NameLookup
from ..rooms import import_rows
from .room_dialog import _LOOKUPS


class BatchRoomsDialog(QDialog):
    def __init__(self, lookup, existing, parent=None):
        super().__init__(parent)
        self.lookup, self.existing = lookup, existing
        self.entries = []
        self.rows, self.waiting, self.running = [], deque(), {}
        self.setWindowTitle("批量添加直播间")
        self.resize(790, 520)
        layout = QVBoxLayout(self)
        guide = QLabel("一行粘贴一个直播间链接，可混合平台。先生成预览，再检查或修改主播名称。保存不会启动监控。")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText("https://live.douyin.com/?live_web_rid=338036396491\nhttps://www.twitch.tv/频道名")
        self.links.setMaximumHeight(105)
        self.links.setAccessibleName("直播间链接，每行一个")
        layout.addWidget(self.links)
        preview = QPushButton("生成预览")
        preview.clicked.connect(self._preview)
        layout.addWidget(preview, alignment=Qt.AlignmentFlag.AlignLeft)
        self.grid = QTableWidget(0, 5)
        self.grid.setHorizontalHeaderLabels(("行 / 链接", "平台", "房间 ID", "主播名称（可编辑）", "状态"))
        self.grid.verticalHeader().hide()
        self.grid.setWordWrap(False)
        self.grid.setAlternatingRowColors(True)
        self.grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.grid, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.save = buttons.addButton("保存可导入项", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.timer = QTimer(self, interval=80)
        self.timer.timeout.connect(self._collect)
        self.links.textChanged.connect(self._invalidate)
        self.finished.connect(self._cancel)

    def _cancel(self):
        self.timer.stop()
        for future, _ in self.running.values():
            future.cancel()
        self.running.clear()
        self.waiting.clear()

    def _invalidate(self):
        self._cancel()
        self.rows = []
        self.grid.setRowCount(0)
        self.save.setEnabled(False)
        self.note.setText("链接已变更，请重新生成预览。")

    def _preview(self):
        self._invalidate()
        raw = self.links.toPlainText()
        if len(raw) > 200_000 or sum(bool(line.strip()) for line in raw.splitlines()) > 200:
            self.note.setText("每批最多 200 条链接，请拆分后重试。")
            return
        self.rows = import_rows(raw, self.existing)
        self.grid.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            values = (f"{row.line} · {row.raw}", NAMES.get(row.platform, "—"), row.identity, "",
                      row.status or "等待查询")
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col != 3 or row.status:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.grid.setItem(i, col, item)
            if not row.status:
                self.waiting.append(i)
        count = len(self.waiting)
        self.note.setText(f"{count} 项可添加，{len(self.rows) - count} 项跳过。留空保存的名称会在监控时补齐。")
        self.save.setEnabled(bool(count))
        if count:
            self.timer.start()
            self._collect()

    def _collect(self):
        for i, (future, deadline) in list(self.running.items()):
            if not future.done() and monotonic() < deadline:
                continue
            lookup = NameLookup(reason="timeout")
            try:
                if future.done():
                    lookup = future.result()
                else:
                    future.cancel()
            except Exception:
                lookup = NameLookup(reason="platform")
            alias = self.grid.item(i, 3)
            name = lookup.name if isinstance(lookup, NameLookup) else lookup
            found = isinstance(name, str) and bool(name.strip())
            if found and not alias.text().strip():
                alias.setText(name.strip()[:128])
            notice = lookup.notice if isinstance(lookup, NameLookup) else NameLookup().notice
            status = "可添加" if found or alias.text().strip() else notice
            self.grid.item(i, 4).setText(status)
            self.grid.item(i, 4).setToolTip(status)
            del self.running[i]
        while self.waiting and len(self.running) < 2:
            i = self.waiting.popleft()
            row = self.rows[i]
            self.running[i] = _LOOKUPS.submit(self.lookup, row.platform, row.identity), monotonic() + 9
            self.grid.item(i, 4).setText("查询中")
        if not self.running and not self.waiting:
            self.timer.stop()
            self.save.setEnabled(any(not row.status for row in self.rows))

    def accept(self):
        if self.save.isEnabled():
            self.entries = [(row.platform, row.identity, self.grid.item(i, 3).text().strip()[:128])
                            for i, row in enumerate(self.rows) if not row.status]
            super().accept()
