import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QLockFile, QTimer

from .catalog import Catalog
from .paths import AppPaths, safe_join
from .platforms import adapter_for
from .settings import PLATFORMS, WindowsCredentials
from .ui.main import MainWindow


def main():
    if sys.argv[1:2] == ["--recording-worker"]:
        from .recording.entry import main as worker_main

        del sys.argv[1]
        worker_main()
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--recordings-dir", type=Path)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-plugins", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_plugins:
        for platform in PLATFORMS:
            print(platform, adapter_for(platform).plugin_class().__name__)
        return 0
    app = QApplication([])
    app.setApplicationName("LiveRecorder Desktop")
    try:
        paths = AppPaths.from_root(args.data_dir) if args.data_dir else AppPaths.default()
        paths.root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(safe_join(paths.root, "desktop.lock")))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.warning(None, "无法打开录像库", "请先从系统托盘退出使用同一数据目录的 LiveRecorder，或检查数据目录是否可写。")
            return 1
        default_media = Path.home() / "Videos" / "LiveRecorder"
        with Catalog(paths.catalog) as catalog:
            window = MainWindow(paths, catalog, WindowsCredentials(), args.recordings_dir or default_media)
            with window.supervisor:
                window.show()
                if args.smoke_test:
                    window.allow_close = True
                    QTimer.singleShot(300, app.quit)
                return app.exec()
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, "LiveRecorder 启动失败", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
