import shutil
import threading
from html import escape
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..catalog import Catalog
from ..compression import Profile, encode, recycle_verified, scan
from ..compression.codec import preflight
from ..compression.recycle import eligible, windows_recycle
from ..platforms import NAMES
from .table import table

STATUSES = {
    "ready": "待压缩", "same": "已是目标编码", "unfinished": "录制中或未完成", "invalid": "无法分析",
    "no_savings": "成品未缩小，原片保留", "success": "已校验成品", "failed": "失败", "cancelled": "已取消",
}


class StreamerPicker(QDialog):
    def __init__(self, rooms, parent=None):
        super().__init__(parent)
        self.rooms = rooms
        self.selected = set()
        self.visible = []
        self.setWindowTitle("按主播选择")
        self.resize(620, 450)
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索已录制主播")
        self.search.setAccessibleName("搜索主播")
        self.platform = QComboBox()
        self.platform.addItem("全部平台", "")
        for key, name in NAMES.items():
            self.platform.addItem(name, key)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.platform)
        layout.addLayout(filters)
        self.view, self.model = table(("主播", "平台"), multi=True)
        layout.addWidget(self.view, 1)
        self.count = QLabel()
        layout.addWidget(self.count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.view.selectionModel().selectionChanged.connect(self._collect)
        self.search.textChanged.connect(self._filter)
        self.platform.currentIndexChanged.connect(self._filter)
        self._filter()

    def _count(self):
        self.count.setText(f"匹配 {len(self.visible)} 个 · 已选 {len(self.selected)} 个")

    def _collect(self, *_):
        self.selected.difference_update(item[0] for item in self.visible)
        self.selected.update(self.visible[index.row()][0] for index in self.view.selectionModel().selectedRows())
        self._count()

    def _filter(self, *_):
        term, platform = self.search.text().casefold(), self.platform.currentData()
        self.visible = [(room, key, name) for room, key, name in self.rooms
                        if (not platform or platform == key) and term in name.casefold()]
        selection = self.view.selectionModel()
        selection.blockSignals(True)
        self.model.replace((name, NAMES[key]) for _, key, name in self.visible)
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for index, (room, _, _) in enumerate(self.visible):
            if room in self.selected:
                selection.select(self.model.index(index, 0), flags)
        selection.blockSignals(False)
        self._count()


def combo(items):
    control = QComboBox()
    for title, value in items:
        control.addItem(title, value)
    return control


def tooltip(*paragraphs):
    body = "".join(
        f'<p style="line-height:145%; margin-top:0px; margin-bottom:{6 if i < len(paragraphs) - 1 else 0}px">'
        f'{escape(text)}</p>'
        for i, text in enumerate(paragraphs)
    )
    return f'<table width="310" cellspacing="0" cellpadding="4"><tr><td>{body}</td></tr></table>'


def explain(control, summary, items):
    control.setToolTip(tooltip(summary))
    for value, text in items.items():
        control.setItemData(control.findData(value), tooltip(text), Qt.ItemDataRole.ToolTipRole)


class CompressionPage(QWidget):
    completed = Signal(object)
    status_changed = Signal(str)

    def __init__(self, catalog, catalog_path):
        super().__init__()
        self.catalog = catalog
        self.catalog_path = catalog_path
        self.paths = []
        self.expected_files = None
        self.candidates = []
        self.excluded = set()
        self.visible = []
        self.destination = None
        self.busy = False
        self.cancel = threading.Event()
        self.setAccessibleName("视频压缩工作区")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 19, 22, 19)
        layout.setSpacing(11)
        title = QLabel("视频压缩")
        title.setObjectName("section")
        layout.addWidget(title)
        intro = QLabel("分析已有视频，再按需压缩。不会修改正在进行的录制。")
        intro.setObjectName("caption")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        sources = QHBoxLayout()
        self.controls = []
        for name, action in (("整个目录", self._choose_folder), ("按主播选择", self._choose_streamers),
                             ("选择视频文件", self._choose_files)):
            button = QPushButton(name)
            button.clicked.connect(action)
            self.controls.append(button)
            sources.addWidget(button)
        self.recursive = QCheckBox("含子目录")
        self.recursive.setChecked(True)
        self.controls.append(self.recursive)
        sources.addWidget(self.recursive)
        sources.addStretch(1)
        layout.addLayout(sources)
        self.source = QLineEdit()
        self.source.setReadOnly(True)
        self.source.setPlaceholderText("尚未选择来源")
        self.source.setAccessibleName("压缩来源")
        layout.addWidget(self.source)
        prefs = QHBoxLayout()
        self.codec = combo([("HEVC / H.265", "hevc"), ("AV1", "av1")])
        self.device = combo([("GPU · NVIDIA", "gpu"), ("CPU · 软件", "cpu")])
        self.preset = combo([("均衡", "balanced"), ("画质优先", "quality"), ("体积优先", "small")])
        self.quality = QSpinBox()
        self.quality.setRange(0, 51)
        self.quality.setSpecialValueText("随预设")
        self.quality.setPrefix("CQ/CRF ")
        self.quality.setAccessibleName("自定义编码质量")
        for name, control in (("目标编码", self.codec), ("编码方式", self.device), ("质量预设", self.preset)):
            control.setAccessibleName(name)
        for control in (self.codec, self.device, self.preset, self.quality):
            prefs.addWidget(control)
            self.controls.append(control)
        self.codec.currentIndexChanged.connect(self._profile_changed)
        self.device.currentIndexChanged.connect(self._profile_changed)
        self.preset.currentIndexChanged.connect(self._profile_changed)
        self.quality.valueChanged.connect(self._update_quality_hint)
        explain(self.codec, "选择成品的视频编码，不改变原片编码。", {
            "hevc": "HEVC（H.265）：兼容设备相对多，适合通用播放与存档。",
            "av1": "AV1：通常适合追求较小体积，旧设备可能不支持硬件解码。",
        })
        explain(self.device, "选择执行编码的设备，不会自动改用另一种方式。", {
            "gpu": "使用 NVIDIA NVENC，通常更快；需要显卡和驱动支持所选编码。",
            "cpu": "使用软件编码器，不要求 NVIDIA 显卡，但通常更慢、CPU 占用更高。",
        })
        explain(self.preset, "质量档位决定「随预设」使用的 CQ 或 CRF。", {
            "balanced": "均衡：在细节与存储大小之间折中。",
            "quality": "画质优先：较低的质量数值，一般保留更多细节，成品可能较大。",
            "small": "体积优先：较高的质量数值，一般更省空间，细节可能更少。",
        })
        layout.addLayout(prefs)
        outputs = QHBoxLayout()
        self.recycle = combo([("保留原片", "keep"), ("每完成一个移入回收站", "each"),
                              ("全部完成后移入回收站", "batch")])
        self.recycle.setAccessibleName("原片保留策略")
        explain(self.recycle, "默认保留原片；回收前要求确认，不会永久删除。", {
            "keep": "成品单独保存，原文件不变。",
            "each": "单片通过校验且更小时，尝试将其原片移入回收站。",
            "batch": "队列内全部成品符合回收条件后，才开始处理这批原片。",
        })
        self.controls.append(self.recycle)
        outputs.addWidget(self.recycle)
        location = QPushButton("成品位置…")
        self.controls.append(location)
        location.clicked.connect(self._choose_destination)
        outputs.addWidget(location)
        self.output_label = QLineEdit("原目录旁的专用成品文件夹")
        self.output_label.setReadOnly(True)
        self.output_label.setAccessibleName("压缩成品目录")
        outputs.addWidget(self.output_label, 1)
        layout.addLayout(outputs)
        self.view, self.model = table(("视频", "源编码", "大小", "状态"), multi=True)
        layout.addWidget(self.view, 1)
        self.status = QLabel("先选择来源，然后分析。")
        self.status.setObjectName("caption")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.scan_button = QPushButton("分析")
        self.scan_button.clicked.connect(self._scan)
        omit = QPushButton("移除选中项")
        omit.clicked.connect(self._omit)
        self.controls.append(omit)
        self.start_button = QPushButton("开始压缩")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._encode)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(lambda: self.cancel.set())
        for button in (self.scan_button, omit, self.start_button, self.cancel_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.completed.connect(self._complete)
        self.status_changed.connect(self.status.setText)
        self._update_quality_hint()
        self._enable()

    def _choose_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "选择视频目录")
        if directory:
            self._source([Path(directory)], directory)

    def _choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频", filter="视频 (*.mp4 *.mkv *.ts *.flv *.mov *.webm *.m4v *.avi *.m2ts *.mts);;所有文件 (*)")
        if files:
            self._source(list(map(Path, files)), f"已选 {len(files)} 个视频文件")

    def _choose_streamers(self):
        candidates = {item.room_id for item in self.catalog.recordings()}
        picker = StreamerPicker([(room.id, room.platform, room.name) for room in self.catalog.rooms(include_hidden=True)
                                 if room.id in candidates], self)
        if picker.exec() and picker.selected:
            items = self.catalog.recordings(sorted(picker.selected))
            expected = {item.path: item.original for item in items if item.is_current()}
            self._source(list(expected), f"已选 {len(picker.selected)} 位主播 · {len(expected)} 个可访问录像"
                         f" · {len(items) - len(expected)} 个失联或已变更", expected)

    def _choose_destination(self):
        choice = QFileDialog.getExistingDirectory(self, "选择成品目录（取消保留现有设置）")
        if choice:
            self.destination = Path(choice)
            self.output_label.setText(choice)

    def _source(self, paths, label, expected=None):
        self.paths = paths
        self.expected_files = expected
        self.source.setText(label)
        self.candidates.clear()
        self.excluded.clear()
        self._render()
        self.status.setText("来源已更新，先分析实际编码。")

    def _profile_changed(self, *_):
        maximum = 63 if self.codec.currentData() == "av1" and self.device.currentData() == "cpu" else 51
        self.quality.setMaximum(maximum)
        self._update_quality_hint()
        self._render()

    def _update_quality_hint(self, *_):
        device = self.device.currentData()
        label = "CQ" if device == "gpu" else "CRF"
        default = Profile(self.codec.currentData(), device, self.preset.currentData()).effective_quality()
        custom = self.quality.value()
        self.quality.setPrefix(label + " ")
        self.quality.setSpecialValueText(f"随预设 · {label} {default}")
        self.quality.setToolTip(tooltip(
            f"视频质量：当前 {label} {custom or default}。",
            f"「随预设」采用当前档位的数值。设为 1–{self.quality.maximum()} 可手动覆盖。",
            "数值越低通常越清晰、文件越大。不同编码器的数值不能直接比较。",
        ))

    def _enable(self):
        for control in self.controls:
            control.setEnabled(not self.busy)
        self.scan_button.setEnabled(not self.busy and bool(self.paths))
        self.start_button.setEnabled(not self.busy and any(item.state == "ready" for item in self.visible))
        self.cancel_button.setEnabled(self.busy)

    def _render(self):
        self.visible = [item.classify(self.codec.currentData()) for item in self.candidates if item.path not in self.excluded]
        self.model.replace((str(item.path), item.video.get("codec_name", "—"),
                            f"{item.snapshot.size / 1024**2:,.1f} MB" if item.snapshot else "—",
                            STATUSES[item.state]) for item in self.visible)
        if self.candidates:
            ready = [item for item in self.visible if item.state == "ready"]
            self.status.setText(f"共 {len(self.visible)} 项，待压缩 {len(ready)} 项 · 源文件合计 "
                                f"{sum(item.snapshot.size for item in ready) / 1024**3:.2f} GB")
        self._enable()

    def _omit(self):
        indices = self.view.selectionModel().selectedRows()
        self.excluded.update(self.visible[index.row()].path for index in indices)
        self._render()

    @staticmethod
    def _tools():
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not (ffmpeg and ffprobe):
            raise ValueError("未找到 FFmpeg 和 FFprobe。请检查 Windows PATH。")
        return Path(ffmpeg), Path(ffprobe)

    def _background(self, fn):
        self.cancel = threading.Event()
        self.busy = True
        self._enable()

        def run():
            try:
                data = fn(self.cancel)
            except (OSError, ValueError, RuntimeError) as error:
                data = ("error", str(error))
            except Exception:
                data = ("error", "后台工作失败，原片处理已停止。")
            self.completed.emit(data)

        threading.Thread(target=run, daemon=True).start()

    def _scan(self):
        try:
            _, ffprobe = self._tools()
        except ValueError as error:
            self.status.setText(str(error))
            return
        paths, target, recursive = self.paths[:], self.codec.currentData(), self.recursive.isChecked()
        expected = self.expected_files
        self.status.setText("正在分析实际编码…")
        self._background(lambda cancel: ("scan", list(scan(paths, target, ffprobe, cancel,
                                                          recursive=recursive, expected=expected))))

    def _encode(self):
        try:
            tools = self._tools()
        except ValueError as error:
            self.status.setText(str(error))
            return
        entries = [item for item in self.visible if item.state == "ready"]
        if not entries:
            return
        profile = Profile(self.codec.currentData(), self.device.currentData(), self.preset.currentData(),
                          self.quality.value() or None)
        mode = self.recycle.currentData()
        transcodes = sum(item.video["codec_name"] in {"hevc", "av1"} for item in entries)
        if transcodes or mode != "keep":
            prompt = QMessageBox(QMessageBox.Icon.Warning, "确认压缩队列", f"共 {len(entries)} 个视频。", parent=self)
            remarks = []
            if transcodes:
                remarks.append(f"{transcodes} 个已是 HEVC 或 AV1，改用另一种编码会再次有损。")
            if mode != "keep":
                remarks.append(self.recycle.currentText() + "。只处理校验成功、体积更小且未变化的原片；"
                               "不支持回收就保留。回收站仍占磁盘空间。")
                prompt.setDetailedText("\n".join(
                    f"{item.snapshot.size / 1024**2:,.1f} MB   {item.path}" for item in entries))
            prompt.setInformativeText("\n".join(remarks))
            prompt.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            prompt.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if prompt.exec() != QMessageBox.StandardButton.Ok:
                return
        destination = self.destination
        database_path = self.catalog_path

        def work(cancel):
            results, recycled, retained, outcomes = [], [], [], {}
            with Catalog(database_path) as history:
                batch = history.begin_batch(profile.codec, mode)
                state = "failed"

                def move(path):
                    history.append_batch_item(batch, {"state": "recycle_requested", "path": str(path)})
                    moved = windows_recycle(path, recheck=lambda: not cancel.is_set() and eligible(outcomes[path]))
                    history.append_batch_item(batch, {"state": "recycled" if moved else "retained", "path": str(path)})
                    return moved

                def collect(report):
                    recycled.extend(report.recycled)
                    retained.extend(report.retained)

                try:
                    if preflight(profile, tools[0], cancel):
                        for i, item in enumerate(entries, 1):
                            if cancel.is_set():
                                break
                            prefix = f"{i}/{len(entries)} · {item.path.name[:44]}"
                            self.status_changed.emit(prefix)
                            result = encode(item, profile, *tools, cancel, destination=destination,
                                            progress=lambda value: self.status_changed.emit(f"{prefix} · {value}"))
                            results.append(result)
                            outcomes[item.path] = result
                            history.append_batch_item(batch, {
                                "state": result.state, "path": str(item.path),
                                "output": str(result.output) if result.output else None,
                                "message": result.message,
                            })
                            if mode == "each":
                                collect(recycle_verified([result], mode, move, cancel))
                        if mode == "batch" and len(results) == len(entries):
                            collect(recycle_verified(results, mode, move, cancel))
                    state = ("cancelled" if cancel.is_set() else "issues"
                             if retained or any(item.state == "failed" for item in results) else "finished")
                finally:
                    history.finish_batch(batch, state)
            return "encode", (results, set(recycled), set(retained))

        self.status.setText("正在验证所选编码器…")
        self._background(work)

    def _complete(self, data):
        self.busy = False
        kind, info = data
        if kind == "scan":
            self.candidates = info
            self.excluded.clear()
            self._render()
            if self.cancel.is_set() or not info:
                self.status.setText("扫描已取消，可重新分析。" if self.cancel.is_set() else "没有找到可识别的视频。")
                self.candidates = []
                self._render()
        elif kind == "encode":
            results, recycled, retained = info
            self.model.replace((str(item.candidate.path), item.candidate.video.get("codec_name", "—"),
                                f"{item.output_snapshot.size / 1024**2:,.1f} MB" if item.output_snapshot else "—",
                                f"{STATUSES[item.state]}{' · 原片已回收' if item.candidate.path in recycled else ''}"
                                f"{' · 原片未回收' if item.candidate.path in retained else ''}"
                                f"{' · ' + item.message if item.message else ''}") for item in results)
            self.status.setText(f"{'已取消 · ' if self.cancel.is_set() else ''}有成品 "
                                f"{sum(result.output is not None for result in results)} 项，原片已回收 "
                                f"{len(recycled)} 项，未完成回收 {len(retained)} 项；重新分析后可启动下一批。")
            self.visible = []
        else:
            self.status.setText(info)
        self._enable()
