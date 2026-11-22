import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

root = Path(os.environ["LIVE_BUILD_ROOT"])
name = os.environ["LIVE_BUILD_NAME"]
a = Analysis(
    [str(root / "packaging" / "windows_entry.py")],
    pathex=[str(root), str(root / ".runtime" / "deps")],
    datas=collect_data_files("streamlink"),
    hiddenimports=[f"streamlink.plugins.{platform}" for platform in
                   ("bilibili", "douyu", "huya", "douyin", "youtube", "twitch")],
)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=False, upx=False)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LiveRecorderWorker", console=True, upx=False)
collect = COLLECT(gui, worker, a.binaries, a.datas, name=name, upx=False)
