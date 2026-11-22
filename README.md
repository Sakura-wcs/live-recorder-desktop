# 原画直播录制（LiveRecorder Desktop）

Windows 桌面录制工具。支持哔哩哔哩、斗鱼、虎牙、抖音、YouTube 和 Twitch。各房间独立启停，默认按平台实际提供的画质保存，不重新编码。

## 下载与快速开始

1. 从 GitHub **Releases** 下载 Windows x64 ZIP，解压整个文件夹，不要只移动 EXE。
2. 安装 [FFmpeg](https://ffmpeg.org/download.html)，让 `ffmpeg.exe` 和 `ffprobe.exe` 都能从系统 PATH 运行。
3. 运行版本化的 `LiveRecorderDesktop-*.exe`，选择录像目录，添加房间并开启任务。批量添加时一行一个链接。

## 从项目目录运行

在本机双击「启动桌面版.cmd」。启动器优先使用 dist 中的 Windows 程序，未打包时使用源码。配置放在本目录的 .runtime\data，初始录像目录为本目录的 Recordings。直接运行打包 EXE（不经启动器）则使用 Windows 用户数据目录和用户视频目录。两种方式的配置目录不同，建议选一种长期使用。界面中可以指定独立的录像根目录；改根目录不搬动旧录像。

新机器可在本目录执行：

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install .
    .\.venv\Scripts\python.exe -m liverecorder

录制需要 Windows PATH 中可用的 ffmpeg.exe 和 ffprobe.exe。首次启动不会自动导入原版房间、Cookie 或其他项目配置。平台配置的凭据使用当前 Windows 用户的凭据管理器。

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

## 构建

    .\.venv\Scripts\python.exe -m pip install pyinstaller
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging/build-windows.ps1

打包是免安装目录。移动时要保留整个 dist\LiveRecorderDesktop 文件夹，不能只复制 EXE；FFmpeg 仍由系统 PATH 提供。

六个平台的插件匹配成功不等于真实房间可录；应先用已获授权且正在开播的房间试录，再用于无人值守。

## 来源、许可与免责声明

本项目独立实现，不包含、不修改、也不调用原版 `LiveRecorder.exe` 或其源码。第三方依赖和参考来源见 [THIRD-PARTY.md](THIRD-PARTY.md)。

本项目原创源码按 [PolyForm Noncommercial 1.0.0](LICENSE.md) 许可公开，**禁止商业使用**。这是源码公开的非商业许可，不是 OSI 定义的开源许可证；第三方组件仍受各自许可证约束。

项目仅供学习和非商业研究。请只录制你拥有权利或已取得授权的内容，并自行遵守适用法律、版权、隐私及平台规则。软件按现状提供，不作保证；在法律许可范围内，作者和贡献者不承担因使用本软件产生的损失、争议或法律责任。
