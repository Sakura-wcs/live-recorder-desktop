# 原画直播录制（LiveRecorder Desktop）

Windows 桌面录制工具。支持哔哩哔哩、斗鱼、虎牙、抖音、YouTube 和 Twitch。各房间独立启停，默认按平台实际提供的画质保存，不重新编码。

## 选择一种运行方式

**只想使用软件？选方式一。** 不需要下载源码、运行 `启动桌面版.cmd`，也不需要自己打包。

### 方式一：下载发行版（推荐）

1. 打开 [Releases](https://github.com/Sakura-wcs/live-recorder-desktop/releases/latest)，在 **Assets（资源）** 中下载 `LiveRecorderDesktop-*-Windows-x64-Setup.exe`。不要下载 GitHub 自动生成的 `Source code (zip)`。
2. 双击安装包，按向导安装，无需解压。安装完成后，在开始菜单搜索「原画直播录制」启动；桌面快捷方式可在安装时选择。

发行版已包含 Python 和界面所需组件，**启动界面不需要另装 Python、PySide6 或构建工具**。默认配置位于 `%LOCALAPPDATA%\LiveRecorderDesktop`，默认录像位于用户视频文件夹下的 `LiveRecorder`。从 Windows「已安装的应用」卸载只移除软件，不删除用户配置或录像。

### 方式二：下载源码，用 CMD 启动（适合开发或修改代码）

1. 在仓库主页选择 **Code → Download ZIP**，解压后进入含 `启动桌面版.cmd` 的目录。
2. 首次运行需要 Python（建议 3.14）。在该目录打开 PowerShell，执行：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install .
   ```

3. 双击 `启动桌面版.cmd`。**从源码运行不等于构建 EXE**，不需要安装 PyInstaller，也不需要下载发行版。此方式默认将配置和录像分别存放在项目目录的 `.runtime\data` 和 `Recordings`。

两种方式的默认配置目录不同，房间列表和设置不会自动同步。选择一种长期使用即可。可以在软件里更改录像目录，更改后不会移动旧录像。

### 两种方式共用：录制与压缩需要 FFmpeg

只打开界面不需要 FFmpeg。需要录制或压缩时，请准备 [FFmpeg](https://ffmpeg.org/download.html)，确保 `ffmpeg.exe` 和 `ffprobe.exe` 都能从系统 PATH 运行；已有则不用重复安装。首次启动不会自动导入原版房间、Cookie 或其他项目配置。平台凭据由 Windows 凭据管理器保存。

## 录制与压缩

- 每行的「启动此房间／停止此房间」只控制这一行；上方的「启动选中／停止选中」也只控制选中的一间。「批量恢复上次监控」会重新启动上次未手动停止的房间。
- 添加房间时可填 ID 或链接。抖音链接中的路径房间号或 live_web_rid 参数会转成数字 ID。名称查询在后台进行，失败可手填。
- 单个添加默认使用上次成功添加的平台。批量添加每行一个链接，可混合六个支持的平台；生成预览后可修改名称，重复项不覆盖，导入后不自动监控。
- 右键单个房间可开启、停止、编辑或删除。按 Ctrl / Shift 多选后，右键仅提供批量删除。编辑或删除前请先停止任务并等待录像封装结束；更换房间 ID 或平台时保留旧房间的录像历史。
- 删除默认只移出房间列表，保留录像和历史。永久删除需要再次核对目录、文件路径及大小；未登记的旧目录、共享目录，或含链接、未登记文件、压缩成品、子目录、身份变化文件的目录均会保留。请先从托盘退出旧版再启动新版；数据库升级后，旧版程序不能打开新版数据库。
- 录制不重编码；优先选 MKV。若 MP4 无法容纳流中的轨道，尝试失败后原画中间文件仍在主播目录，文件名带 .partial。断线后的可用片段会独立封装。
- 压缩页先选择整个目录、已录制主播或视频文件，再分析编码并审阅清单。成品默认为源目录内的 LiveRecorder_压缩成品 子目录。支持 HEVC/AV1 的 CPU 和 NVIDIA GPU 方式；两种回收模式都是显式确认，默认保留原片，不能回收的文件会保留。
- 「随预设」会显示当前实际 CQ 或 CRF，手动输入数字则覆盖预设的质量值。将鼠标停在选项上可查看说明。
- 压缩会校验色彩及轨道，含部分复杂旋转或 HDR 元数据、封面、多画面轨的视频会保守跳过或拒绝，原片不受影响。

<details>
<summary>开发者：如何自行构建 Windows 发行版</summary>

仅在需要生成自己的 EXE 时使用。先完成上面「方式二」的环境准备，再运行：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/build-windows.ps1
```

产物位于 `dist\LiveRecorderDesktop`，分发时要保留整个目录。普通用户直接下载发行版即可。

</details>

六个平台的插件匹配成功不等于真实房间可录；应先用已获授权且正在开播的房间试录，再用于无人值守。

## 来源、许可与免责声明

本项目独立实现，不包含、不修改、也不调用原版 `LiveRecorder.exe` 或其源码。第三方依赖和参考来源见 [THIRD-PARTY.md](THIRD-PARTY.md)。

本项目原创源码按 [PolyForm Noncommercial 1.0.0](LICENSE.md) 许可公开，**禁止商业使用**。这是源码公开的非商业许可，不是 OSI 定义的开源许可证；第三方组件仍受各自许可证约束。

项目仅供学习和非商业研究。请只录制你拥有权利或已取得授权的内容，并自行遵守适用法律、版权、隐私及平台规则。软件按现状提供，不作保证；在法律许可范围内，作者和贡献者不承担因使用本软件产生的损失、争议或法律责任。
