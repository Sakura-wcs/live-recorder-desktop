import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import NamedTemporaryFile

from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSpinBox, QStackedWidget, QStyle, QSystemTrayIcon, QTabWidget,
                               QVBoxLayout, QWidget)

from ..catalog import Catalog
from ..exchange import export_recordings, export_rooms, import_rooms
from ..media_tools import diagnose_tools, resolve_tools
from ..paths import checked_path, require_recording_volume, safe_join
from ..platforms import NAMES, adapter_for
from ..recording.service import RecordingService
from ..recording.recovery import RecordingJournal
from ..rooms import plan_removal, remove_directory
from ..settings import PlatformSettings
from .batch_rooms import BatchRoomsDialog
from .compression import CompressionPage
from .room_dialog import RoomDialog
from .table import ActionCell, table
from .theme import STYLESHEET


def caption(text):
    label = QLabel(text)
    label.setObjectName("caption")
    label.setWordWrap(True)
    return label


def page(title, description):
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(22, 19, 22, 19)
    layout.setSpacing(11)
    heading = QLabel(title)
    heading.setObjectName("section")
    layout.addWidget(heading)
    layout.addWidget(caption(description))
    return widget, layout


class FileOperationDialog(QDialog):
    def reject(self):
        pass

    def closeEvent(self, event):
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, paths, catalog, credential_store, recordings: Path | None):
        super().__init__()
        self.paths, self.catalog = paths, catalog
        self.settings = PlatformSettings(paths.platforms, credential_store)
        self.preference_path = safe_join(paths.root, "app.json")
        self.recordings = paths.recording_root(recordings) if recordings else None
        self.tool_preferences = {}
        self.history_before = [None]
        self.history_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recording-history")
        self.history_future = None
        self.container = "mkv"
        self.room_limit = 8
        self.last_platform = None
        preferences_error = False
        if self.preference_path.is_file():
            try:
                data = json.loads(self.preference_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("配置版本无效")
                if data.get("recordings"):
                    self.recordings = paths.recording_root(Path(data["recordings"]))
                self.tool_preferences = {key: data[key] for key in ("ffmpeg_path", "ffprobe_path")
                                         if isinstance(data.get(key), str)}
                self.container = "mp4" if data.get("container") == "mp4" else "mkv"
                self.room_limit = max(1, min(32, int(data.get("room_limit", 8))))
                self.last_platform = data.get("last_platform")
            except (OSError, ValueError, TypeError):
                preferences_error = True
                self.recordings = None
        self.recording = RecordingService(self.catalog, self._command, self.room_limit)
        self.supervisor = self.recording.supervisor
        interrupted = self.catalog.flag_interrupted_batches()
        recovered = RecordingJournal(self.catalog).reconcile()
        self.allow_close = False
        self.exiting = False
        self.file_operation = False
        self.room_states = {}
        self.visible_rooms = []
        self.setWindowTitle("LiveRecorder Desktop")
        self.setMinimumSize(730, 520)
        self.resize(1040, 680)
        self.setStyleSheet(STYLESHEET)
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        rail = QWidget()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(158)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(11, 19, 11, 13)
        mark = QLabel("LIVE RECORDER")
        mark.setObjectName("eyebrow")
        rail_layout.addWidget(mark)
        rail_layout.addWidget(caption("本地直播录制"))
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(["录制", "视频压缩", "平台设置"])
        rail_layout.addWidget(self.nav, 1)
        rail_layout.addWidget(caption("Windows · 原画保存"))
        layout.addWidget(rail)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(body)
        self.stack.addWidget(self._recording_page())
        self.compression = CompressionPage(self.catalog, self.paths.catalog)
        self.compression.tool_preferences = lambda: dict(self.tool_preferences)
        self.stack.addWidget(self.compression)
        self.stack.addWidget(self._platform_page())
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._refresh_rooms()
        self._refresh_history()
        if interrupted or recovered:
            self.status.setText(f"补登录像 {recovered} 项；中断压缩批次 {interrupted} 个，未重放原片回收。")
        if preferences_error:
            self.status.setText("无法读取原录像设置。请选择非系统盘录像目录后再启动任务。")
        elif self.recordings:
            try:
                require_recording_volume(self.recordings)
            except (OSError, ValueError) as error:
                self.status.setText(str(error))
        self._setup_tray()
        self.poll = QTimer(self)
        self.poll.setInterval(300)
        self.poll.timeout.connect(self._poll)
        self.poll.start()

    def _recording_page(self):
        widget, layout = page("录制", "保存常用直播间，按房间独立启停。录像保留源画质。")
        directory = QHBoxLayout()
        self.root_display = QLineEdit(str(self.recordings) if self.recordings else "")
        self.root_display.setPlaceholderText("请选择非系统盘录像目录（不会回退到 C 盘）")
        self.root_display.setReadOnly(True)
        self.root_display.setAccessibleName("当前录像根目录")
        directory.addWidget(self.root_display, 1)
        root_pick = QPushButton("选择录像目录")
        root_pick.clicked.connect(self._choose_root)
        directory.addWidget(root_pick)
        self.container_picker = QComboBox()
        self.container_picker.addItems(["MKV", "MP4"])
        self.container_picker.setCurrentText(self.container.upper())
        self.container_picker.setAccessibleName("录像封装格式")
        self.container_picker.currentTextChanged.connect(self._set_container)
        directory.addWidget(self.container_picker)
        layout.addLayout(directory)
        tabs = QTabWidget()
        rooms_widget = QWidget()
        rooms = QVBoxLayout(rooms_widget)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索主播、平台或房间号")
        self.search.setAccessibleName("搜索直播间")
        self.search.textChanged.connect(self._refresh_rooms)
        tools.addWidget(self.search, 1)
        add = QPushButton("添加房间")
        add.setObjectName("primary")
        add.clicked.connect(self._add_room)
        tools.addWidget(add)
        batch = QPushButton("批量添加")
        batch.clicked.connect(self._batch_add_rooms)
        tools.addWidget(batch)
        self.start_button = QPushButton("启动选中")
        self.start_button.setToolTip("只启动表内选中的一个直播间。")
        self.start_button.clicked.connect(self._start)
        tools.addWidget(self.start_button)
        self.stop_button = QPushButton("停止选中")
        self.stop_button.setToolTip("只停止表内选中的一个直播间。")
        self.stop_button.clicked.connect(self._stop)
        tools.addWidget(self.stop_button)
        rooms.addLayout(tools)
        self.room_table, self.room_model = table(("主播", "平台", "房间", "状态", "单房操作"), multi=True)
        self.room_table.setItemDelegateForColumn(4, ActionCell(self.room_table))
        self.room_table.clicked.connect(self._row_action)
        self.room_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.room_table.customContextMenuRequested.connect(self._room_context)
        self.room_table.selectionModel().selectionChanged.connect(self._update_room_buttons)
        rooms.addWidget(self.room_table)
        self.room_count = caption("")
        rooms.addWidget(self.room_count)
        tabs.addTab(rooms_widget, "房间")
        self.history_table, self.history_model = table(("主播", "录像路径", "状态"))
        history = QWidget()
        history_layout = QVBoxLayout(history)
        history_layout.addWidget(self.history_table, 1)
        history_tools = QHBoxLayout()
        for label, callback in (("较新", self._history_newer), ("较早", self._history_older),
                                ("恢复未完成录像", self._recover_recordings)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            history_tools.addWidget(button)
        history_tools.addStretch(1)
        history_layout.addLayout(history_tools)
        tabs.addTab(history, "录像历史")
        layout.addWidget(tabs, 1)
        self.status = caption("")
        layout.addWidget(self.status)
        resume = QPushButton("批量恢复上次监控")
        resume.setToolTip("启动上次未手动停止的房间；不是单房操作。")
        resume.clicked.connect(self._resume)
        layout.addWidget(resume, 0, Qt.AlignmentFlag.AlignLeft)
        exchange = QHBoxLayout()
        for label, callback in (("导入房间 JSON", self._import_rooms), ("导出房间 JSON", self._export_rooms),
                                ("导出录像 CSV", self._export_history)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            exchange.addWidget(button)
        exchange.addStretch(1)
        layout.addLayout(exchange)
        return widget

    def _platform_page(self):
        widget, layout = page("平台设置", "每个平台的房间共享同一份设置。新检测和重连会使用更新后的设置。")
        form = QFormLayout()
        self.platform_picker = QComboBox()
        for key, name in NAMES.items():
            self.platform_picker.addItem(name, key)
        self.proxy = QLineEdit()
        self.proxy.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.proxy.setPlaceholderText("可留空，例如 http://127.0.0.1:7890")
        self.cookie = QLineEdit()
        self.cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.cookie.setPlaceholderText("可留空，不会读取浏览器的数据")
        self.cookie_label = QLabel("Cookie")
        self.limit_picker = QSpinBox()
        self.limit_picker.setRange(1, 32)
        self.limit_picker.setValue(self.room_limit)
        self.limit_picker.setMaximumWidth(100)
        self.limit_picker.setAccessibleName("同时监控房间上限")
        self.limit_picker.valueChanged.connect(self._set_limit)
        form.addRow("同时监控上限", self.limit_picker)
        form.addRow("平台", self.platform_picker)
        form.addRow("代理", self.proxy)
        form.addRow(self.cookie_label, self.cookie)
        self.tool_paths = {}
        for key, label in (("ffmpeg_path", "FFmpeg"), ("ffprobe_path", "FFprobe")):
            field = QLineEdit(self.tool_preferences.get(key, ""))
            field.setPlaceholderText("留空使用系统 PATH；或输入完整 EXE 路径")
            form.addRow(label, field)
            self.tool_paths[key] = field
        layout.addLayout(form)
        self.settings_notice = caption("")
        layout.addWidget(self.settings_notice)
        save = QPushButton("保存平台配置")
        save.setObjectName("primary")
        save.clicked.connect(self._save_platform)
        layout.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        tools = QPushButton("检测并保存媒体工具")
        tools.clicked.connect(self._diagnose_media)
        layout.addWidget(tools, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.platform_picker.currentIndexChanged.connect(self._load_platform)
        self._load_platform()
        return widget

    def _load_platform(self):
        platform = self.platform_picker.currentData()
        supports_cookie = "cookie" in adapter_for(platform).fields
        self.cookie.setVisible(supports_cookie)
        self.cookie_label.setVisible(supports_cookie)
        self.proxy.clear()
        self.cookie.clear()
        try:
            values = self.settings.load(platform)
            self.proxy.setText(values.get("proxy", ""))
            self.cookie.setText(values.get("cookie", ""))
            hint = "登录状态可能影响可选画质。" if platform == "bilibili" else ""
            self.settings_notice.setText(hint + "Cookie 和带密码的代理存入 Windows 凭据管理器。")
        except (OSError, ValueError):
            self.settings_notice.setText("无法读取此平台配置，请重新填写。")

    def _save_platform(self):
        platform = self.platform_picker.currentData()
        values = {"proxy": self.proxy.text().strip()}
        if "cookie" in adapter_for(platform).fields:
            values["cookie"] = self.cookie.text().strip()
        try:
            self.settings.save(platform, values)
        except (OSError, ValueError):
            self.settings_notice.setText("保存失败。请检查代理格式或 Windows 凭据管理器。")
        else:
            self.settings_notice.setText(f"{NAMES[platform]}设置已保存。")

    def _save_preferences(self, recordings, container, *, limit=None, last_platform=None, tools=None):
        new_path = checked_path(self.preference_path)
        staging = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=new_path.parent, suffix=".tmp", delete=False) as file:
                staging = Path(file.name)
                json.dump({"version": 1, "recordings": str(recordings) if recordings else None, "container": container,
                           **(self.tool_preferences if tools is None else tools),
                           "room_limit": self.room_limit if limit is None else limit,
                           "last_platform": last_platform or self.last_platform},
                          file, ensure_ascii=False)
            checked_path(staging).replace(checked_path(new_path))
        finally:
            if staging is not None:
                checked_path(staging).unlink(missing_ok=True)

    def _choose_root(self):
        chosen = QFileDialog.getExistingDirectory(self, "选择录像根目录", str(self.recordings or ""))
        if not chosen:
            return
        try:
            root = require_recording_volume(self.paths.recording_root(Path(chosen)))
            self._save_preferences(root, self.container)
        except (OSError, ValueError) as error:
            self.status.setText(f"不能使用该目录：{error}。请选择可写的非系统盘目录。")
            return
        self.recordings = root
        self.root_display.setText(str(root))
        self.status.setText("已保存：新启动的房间使用新目录，历史录像不搬动。")

    def _set_container(self, name):
        try:
            self._save_preferences(self.recordings, name.lower())
        except (OSError, ValueError):
            self.container_picker.blockSignals(True)
            self.container_picker.setCurrentText(self.container.upper())
            self.container_picker.blockSignals(False)
            self.status.setText("保存封装设置失败。")
        else:
            self.container = name.lower()
            self.status.setText("已保存：新启动的房间采用此封装。")

    def _set_limit(self, limit):
        try:
            self._save_preferences(self.recordings, self.container, limit=limit)
        except (OSError, ValueError):
            self.limit_picker.blockSignals(True)
            self.limit_picker.setValue(self.room_limit)
            self.limit_picker.blockSignals(False)
            self.settings_notice.setText("未能保存监控上限。")
        else:
            self.room_limit = limit
            self.supervisor.set_limit(limit)
            self.settings_notice.setText("并发上限已保存，不中断目前的监控。")

    def _selected_rooms(self):
        return [self.visible_rooms[index.row()] for index in self.room_table.selectionModel().selectedRows()]

    def _selected_room(self):
        rooms = self._selected_rooms()
        return rooms[0] if len(rooms) == 1 else None

    def _update_room_buttons(self, *_):
        room = self._selected_room()
        active = room is not None and self.supervisor.active(room.id)
        self.start_button.setEnabled(not self.exiting and room is not None and not active
                                     and not self.supervisor.pending(room.id))
        self.stop_button.setEnabled(not self.exiting and active)

    def _row_action(self, index):
        if index.column() != 4 or self.exiting:
            return
        room = self.visible_rooms[index.row()]
        if self.supervisor.active(room.id):
            self._stop(room)
        else:
            self._start(room)

    def _refresh_rooms(self):
        selected = {room.id for room in self._selected_rooms()}
        term = self.search.text().strip().casefold()
        all_rooms = self.catalog.rooms()
        self.visible_rooms = [room for room in all_rooms
                              if term in f"{room.name} {NAMES[room.platform]} {room.identity}".casefold()]
        self.room_model.replace(
            (room.name or f"{room.identity}（待补名）", NAMES[room.platform], room.identity, self.room_states.get(room.id, "未启动"),
             "停止此房间" if self.supervisor.active(room.id) else "启动此房间")
            for room in self.visible_rooms
        )
        selection = QItemSelection()
        for row, room in enumerate(self.visible_rooms):
            if room.id in selected:
                index = self.room_model.index(row, 0)
                selection.select(index, index)
        flags = QItemSelectionModel.SelectionFlag
        self.room_table.selectionModel().select(selection, flags.ClearAndSelect | flags.Rows)
        self.room_count.setText(f"显示 {len(self.visible_rooms)} / {len(all_rooms)} 个房间")
        self._update_room_buttons()

    def _refresh_history(self):
        names = {room.id: room.display_name for room in self.catalog.rooms(include_hidden=True)}
        entries = self.catalog.recordings(limit=101, before=self.history_before[-1])
        self.history_next = entries[99].id if len(entries) > 100 else None
        entries = entries[:100]
        if self.history_future:
            self.history_future.cancel()
        rows = [(names.get(item.room_id, "未知主播"), str(item.path), "正在核对") for item in entries]
        self.history_model.replace(rows)
        self.history_future = self.history_pool.submit(lambda: [
            (name, path, "已保存" if item.is_current() else "原文件失联、变更或未登记")
            for (name, path, _), item in zip(rows, entries, strict=True)])

    def _history_newer(self):
        if len(self.history_before) > 1:
            self.history_before.pop()
            self._refresh_history()

    def _history_older(self):
        if self.history_next is not None:
            self.history_before.append(self.history_next)
            self._refresh_history()

    def _diagnose_media(self):
        prefs = {key: field.text().strip() for key, field in self.tool_paths.items()}
        try:
            result = self._run_file_operation("检测媒体工具", lambda: diagnose_tools(prefs))
            self._save_preferences(self.recordings, self.container, tools=prefs)
        except (OSError, ValueError) as error:
            self.settings_notice.setText(str(error))
            return
        self.tool_preferences = prefs
        encoders = ", ".join(name for name in ("hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1")
                             if name in result.encoders) or "未发现支持的编码器"
        self.settings_notice.setText(f"{result.ffmpeg} · {result.ffmpeg_version}\n"
                                     f"{result.ffprobe} · {result.ffprobe_version}\n"
                                     f"编译支持：{encoders}。实际可用性以编码预检为准。")

    def _import_rooms(self):
        picked, _ = QFileDialog.getOpenFileName(self, "导入房间清单", "", "JSON (*.json)")
        if not picked:
            return
        try:
            entries = import_rooms(Path(picked))
            if QMessageBox.question(self, "导入房间", f"读取到 {len(entries)} 个房间。跳过重复项，不自动开始监控。",
                                    defaultButton=QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                self._save_new_rooms(entries)
        except (OSError, ValueError) as error:
            self.status.setText(f"导入失败：{error}")

    def _export_rooms(self):
        picked, _ = QFileDialog.getSaveFileName(self, "导出房间清单", "直播间.json", "JSON (*.json)")
        if picked:
            try:
                count = export_rooms(Path(picked), self.catalog.rooms(), overwrite=True)
                self.status.setText(f"已导出 {count} 个房间。不包含平台配置或凭据。")
            except (OSError, ValueError) as error:
                self.status.setText(f"导出失败：{error}")

    def _export_history(self):
        picked, _ = QFileDialog.getSaveFileName(self, "导出录像清单", "录像历史.csv", "CSV (*.csv)")
        if not picked:
            return
        catalog_path = self.paths.catalog

        def work():
            with Catalog(catalog_path) as db:
                return export_recordings(Path(picked), db.rooms(include_hidden=True), db.recordings(), overwrite=True)
        try:
            count = self._run_file_operation("导出录像清单", work)
            self.status.setText(f"已导出 {count} 条录像，只含文件名，不含目录。")
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.setText(f"导出失败：{error}")

    def _recover_recordings(self):
        if self.exiting or self.file_operation or self.supervisor.pending() or self.compression.busy:
            self.status.setText("请先停止监控与压缩，再恢复未完成录像。")
            return
        items = RecordingJournal(self.catalog).retryable()[:100]
        if not items:
            self.status.setText("未发现身份可核实且可以重试封装的原始片段。")
            return
        labels = [f"{item.id} · {item.raw.name}" for item in items]
        picked, ok = QInputDialog.getItem(self, "恢复未完成录像", "选择片段（不会删除原始文件）", labels, 0, False)
        if not ok:
            return
        item = items[labels.index(picked)]
        catalog_path = self.paths.catalog
        try:
            tools = resolve_tools(self.tool_preferences)

            def work():
                with Catalog(catalog_path) as db:
                    return RecordingJournal(db).recover(item, *tools)

            target = self._run_file_operation("恢复封装", work)
            self.status.setText(f"已恢复：{target.name}。原始片段已保留。")
            self._refresh_history()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self.status.setText(f"恢复未完成，原片保留：{error}")

    def _lookup_name(self, platform, identity):
        return adapter_for(platform).lookup_name(identity, self.settings.load(platform))

    def _save_new_rooms(self, entries):
        try:
            added = self.catalog.add_missing_rooms(entries)
        except sqlite3.Error:
            self.status.setText("未能保存房间，请检查录像库是否可写。")
            return 0
        notice = f"新增 {len(added)} 个房间，跳过 {len(entries) - len(added)} 个重复房间。未启动监控。"
        if added:
            self.last_platform = added[-1][0]
            try:
                self._save_preferences(self.recordings, self.container)
            except (OSError, ValueError):
                notice += " 默认平台设置未能保存。"
            self._refresh_rooms()
        self.status.setText(notice)
        return len(added)

    def _add_room(self):
        dialog = RoomDialog(self._lookup_name, self, platform=self.last_platform)
        if dialog.exec():
            key, identity = dialog.identity
            self._save_new_rooms([(key, identity, dialog.alias.text().strip())])
        dialog.deleteLater()

    def _batch_add_rooms(self):
        existing = {(room.platform, room.identity) for room in self.catalog.rooms()}
        dialog = BatchRoomsDialog(self._lookup_name, existing, self)
        if dialog.exec():
            self._save_new_rooms(dialog.entries)
        dialog.deleteLater()

    def _context_targets(self, index):
        if not index.isValid():
            return []
        selection = self.room_table.selectionModel()
        if not selection.isRowSelected(index.row()):
            flags = QItemSelectionModel.SelectionFlag
            selection.setCurrentIndex(index, flags.ClearAndSelect | flags.Rows)
        return self._selected_rooms()

    def _build_room_menu(self, rooms):
        menu = QMenu(self)
        pending = any(self.supervisor.pending(room.id) for room in rooms)
        if len(rooms) == 1:
            room = rooms[0]
            menu.addAction("开启任务", lambda: self._start(room)).setEnabled(not pending)
            menu.addAction("停止任务", lambda: self._stop(room)).setEnabled(self.supervisor.active(room.id))
            menu.addAction("编辑配置", lambda: self._edit_room(room)).setEnabled(not pending)
            menu.addSeparator()
        delete = "删除房间" if len(rooms) == 1 else f"删除 {len(rooms)} 个房间"
        menu.addAction(delete, lambda: self._delete_rooms(rooms)).setEnabled(not pending)
        return menu

    def _room_context(self, point):
        rooms = self._context_targets(self.room_table.indexAt(point))
        if rooms and not self.exiting:
            menu = self._build_room_menu(rooms)
            menu.exec(self.room_table.viewport().mapToGlobal(point))
            menu.deleteLater()

    def _edit_room(self, room):
        def save(platform, identity, name):
            if self.exiting or self.supervisor.pending(room.id):
                raise ValueError("请先停止该任务，并等待录像封装完成。")
            try:
                self.catalog.edit_room(room.id, platform, identity, name)
            except sqlite3.Error as error:
                raise ValueError("未能保存房间配置。") from error

        dialog = RoomDialog(self._lookup_name, self, room=room, validate=save)
        dialog.note.setText("更换平台或房间 ID 会保留旧房间的录像历史，新房间不会自动启动。")
        if dialog.exec():
            self._refresh_rooms()
            self._refresh_history()
            self.status.setText("房间配置已更新。历史录像不会搬动。")
        dialog.deleteLater()

    def _run_file_operation(self, title, work):
        dialog = FileOperationDialog(self)
        dialog.setWindowTitle(title)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        layout = QVBoxLayout(dialog)
        layout.addWidget(caption("正在处理，请稍候。其他房间的监控仍会继续。"))
        self.file_operation = True
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(work)
                timer = QTimer(dialog, interval=100)
                timer.timeout.connect(lambda: dialog.accept() if future.done() else None)
                timer.start()
                dialog.exec()
                timer.stop()
                return future.result()
        finally:
            self.file_operation = False
            dialog.deleteLater()

    def _delete_rooms(self, rooms):
        ids = {room.id for room in rooms}
        if not ids or self.exiting or self.file_operation or any(self.supervisor.pending(i) for i in ids):
            self.status.setText("请先停止选中的任务，并等待录像封装完成。")
            return
        choice = QMessageBox(QMessageBox.Icon.Question, "删除房间",
                             f"如何处理选中的 {len(ids)} 个房间？仅从列表移除会保留录像和历史。", parent=self)
        archive = choice.addButton("仅从列表移除", QMessageBox.ButtonRole.AcceptRole)
        purge = choice.addButton("永久删除已录制内容…", QMessageBox.ButtonRole.DestructiveRole)
        cancel = choice.addButton(QMessageBox.StandardButton.Cancel)
        choice.setDefaultButton(archive)
        choice.setEscapeButton(cancel)
        purge.setEnabled(not self.compression.busy)
        purge.setToolTip("压缩分析或编码进行中时不能删除录像。")
        choice.exec()
        try:
            if choice.clickedButton() is archive:
                self.catalog.archive(ids)
                self.status.setText(f"已从列表移除 {len(ids)} 个房间。磁盘录像和历史均已保留。")
            elif choice.clickedButton() is purge and not self.compression.busy:
                self._purge_rooms(ids)
            else:
                return
        except (OSError, ValueError, sqlite3.Error):
            self.status.setText("未能完成操作，未确认成功处理的房间不会从列表移除。")
        self._refresh_rooms()
        self._refresh_history()

    def _purge_rooms(self, ids):
        catalog_path, data_root = self.paths.catalog, self.paths.root

        def scan():
            with Catalog(catalog_path) as catalog:
                return plan_removal(catalog, ids, protected=(data_root,))

        plans, skipped = self._run_file_operation("检查录像目录", scan)
        blocked = {room for room, paths in skipped.items() if paths}
        # A blocked room is left intact even if some of its directories qualify.
        plans = [plan for plan in plans if plan.room not in blocked]
        details = []
        for plan in plans:
            details.append(f"{plan.path} ｜ {len(plan.files)} 个文件 ｜ {plan.size:,} 字节")
            details.extend(f"  {path} ｜ {stamp[2]:,} 字节" for path, stamp in plan.files)
        if blocked:
            details += ["", "以下路径无法核实或不独占；对应房间及其全部目录将保留："]
            details += [str(path) for room in sorted(blocked) for path in sorted(skipped[room])]
        total = sum(plan.size for plan in plans)
        files = sum(len(plan.files) for plan in plans)
        eligible = {plan.room for plan in plans}
        if not plans:
            box = QMessageBox(QMessageBox.Icon.Information, "没有可永久删除的目录",
                              "未找到可核实的独占目录。未删除任何录像，也未移除房间；可改用“仅从列表移除”。",
                              parent=self)
            if details:
                box.setDetailedText("\n".join(details))
            box.exec()
            return
        confirmation = QMessageBox(QMessageBox.Icon.Warning, "再次确认永久删除",
                                   f"永久删除 {len(plans)} 个目录、{files} 个文件（{total:,} 字节），"
                                   f"涉及 {len(eligible)} 个房间。请展开详细信息核对完整路径。此操作无法恢复。",
                                   parent=self)
        confirmation.setDetailedText("\n".join(details))
        confirm = confirmation.addButton("永久删除上述目录", QMessageBox.ButtonRole.DestructiveRole)
        cancel = confirmation.addButton(QMessageBox.StandardButton.Cancel)
        confirmation.setDefaultButton(cancel)
        confirmation.setEscapeButton(cancel)
        confirmation.exec()
        if (confirmation.clickedButton() is not confirm or self.compression.busy or self.exiting
                or any(self.supervisor.pending(i) for i in ids)):
            return

        def erase():
            current, refused = scan()
            by_path = {plan.path: plan for plan in current}
            failed_rooms = {plan.room for plan in plans if refused[plan.room] or by_path.get(plan.path) != plan}
            succeeded, failed = [], []
            for plan in plans:
                if plan.room in failed_rooms:
                    failed.append(plan)
                    continue
                try:
                    remove_directory(plan)
                except (OSError, ValueError):
                    failed_rooms.add(plan.room)
                    failed.append(plan)
                else:
                    succeeded.append(plan)
            return succeeded, failed

        succeeded, failed = self._run_file_operation("永久删除录像", erase)
        for plan in succeeded:
            self.catalog.forget_directory(plan.room, plan.path)
        removed = eligible - {plan.room for plan in failed}
        self.catalog.archive(removed)
        self.status.setText(f"已永久删除 {len(succeeded)} 个目录，移除 {len(removed)} 个房间；"
                            f"保留 {len(ids - removed)} 个房间。")
        if failed:
            box = QMessageBox(QMessageBox.Icon.Warning, "部分目录未删除",
                              "部分目录发生变化、被占用或无法访问。对应房间仍在列表中；出错前已删除的文件无法恢复。",
                              parent=self)
            box.setDetailedText("\n".join(str(plan.path) for plan in failed))
            box.exec()

    def _command(self, room_id):
        if not self.recordings:
            raise ValueError("请先选择非系统盘录像目录，程序不会使用 C 盘作为缓存。")
        require_recording_volume(self.recordings)
        ffmpeg, ffprobe = map(str, resolve_tools(self.tool_preferences))
        launch = ([str(Path(sys.executable).with_name("LiveRecorderWorker.exe")), "--recording-worker"]
                  if getattr(sys, "frozen", False) else [sys.executable, "-m", "liverecorder.recording.entry"])
        return [*launch, str(room_id),
                str(self.paths.root), str(self.recordings), ffmpeg, ffprobe, "--container", self.container]

    def _start(self, room=None):
        if not room:
            room = self._selected_room()
        if room is None:
            self.status.setText("请先在列表选中一个房间。")
            return
        if self.exiting or self.file_operation or self.supervisor.pending(room.id):
            return
        try:
            started = self.recording.start(room)
        except sqlite3.Error:
            self.status.setText("录像库暂不可写，无法保存监控状态。")
            return
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        if started:
            self.room_states[room.id] = "启动中"
            self._refresh_rooms()
        else:
            self.status.setText("房间已在监控，或已达并发上限。")

    def _stop(self, room=None):
        if not room:
            room = self._selected_room()
        if room is not None:
            try:
                self.recording.stop(room)
            except sqlite3.Error:
                self.status.setText("任务已收到停止指令，但未能保存停止状态。")
            self.room_states[room.id] = "等待结束" if self.supervisor.active(room.id) else "未启动"
            self._refresh_rooms()

    def _resume(self):
        if self.exiting or self.file_operation:
            return
        count = 0
        try:
            for room in self.recording.resume():
                self.room_states[room.id] = "启动中"
                count += 1
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
        else:
            self.status.setText(f"本次恢复 {count} 个房间。已启动或超过并发上限的房间不会重复启动。")
        self._refresh_rooms()

    def _poll(self):
        if self.history_future and self.history_future.done():
            self.history_model.replace(self.history_future.result())
            self.history_future = None
        events = self.supervisor.events()
        if events:
            for event in events:
                if event.state == "name_filled":
                    self._refresh_history()
                    continue
                self.room_states[event.room_id] = event.message
                if event.state == "finished":
                    self._refresh_history()
            self._refresh_rooms()
        if (self.exiting and not self.compression.busy
                and not self.supervisor.pending()):
            self.allow_close = True
            QApplication.instance().quit()

    def _setup_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        self.tray = QSystemTrayIcon(QIcon(icon), self)
        self.tray.setToolTip("LiveRecorder Desktop")
        menu = QMenu(self)
        menu.addAction("显示窗口", self.showNormal)
        menu.addAction("退出", self._exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.showNormal()
                                    if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def _exit(self):
        if self.file_operation:
            return
        active = [room for room in self.catalog.rooms() if self.supervisor.active(room.id)]
        if (active or self.compression.busy) and QMessageBox.question(
            self, "退出", "停止所有监控和压缩任务，并等待当前工作安全结束？"
        ) != QMessageBox.StandardButton.Yes:
            return
        for room in active:
            self.supervisor.stop(room.id)
        self.compression.cancel.set()
        self.exiting = True
        self.status.setText("正在停止监控并收尾…")
        self.start_button.setEnabled(False)
        self._poll()

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
        else:
            event.ignore()
            if self.tray is not None and not self.exiting:
                self.hide()
            elif not self.exiting:
                self._exit()
