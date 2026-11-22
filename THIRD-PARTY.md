# 第三方软件与参考来源

- [Streamlink](https://github.com/streamlink/streamlink)（BSD-2-Clause）：通过独立依赖调用平台插件；不复制或改写插件源码。
- [Qt for Python / PySide6 和 Shiboken6](https://doc.qt.io/qtforpython-6/licenses.html)：所用模块按 LGPLv3 适用条款分发。版本及许可文件见 Windows 包中的 `third-party`。对应 PySide/Shiboken 源码见 [官方仓库](https://code.qt.io/cgit/pyside/pyside-setup.git/) 的相同版本标签，Qt 源码见 [官方版本目录](https://download.qt.io/official_releases/qt/)。动态库位于 `_internal`，可用兼容的修改版替换；应用许可不限制为调试这些修改而进行的逆向工程。
- [pywin32](https://github.com/mhammond/pywin32) 和 [PyInstaller](https://github.com/pyinstaller/pyinstaller)：分别用于 Windows 接口及打包，遵循上游许可。
- [FFmpeg / FFprobe](https://ffmpeg.org/)：独立运行；发布包不携带其二进制。
- [auqhjjqdo/LiveRecorder v2.35](https://github.com/auqhjjqdo/LiveRecorder)（MIT）：仅参考使用流程。未复制、集成或调用其源码或 `LiveRecorder.exe`，也不读取其配置。
