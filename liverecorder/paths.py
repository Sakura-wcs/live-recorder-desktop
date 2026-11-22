import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEVICE = re.compile(r"(?i)^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$")


def safe_component(value: str) -> str:
    value = _INVALID.sub("_", unicodedata.normalize("NFC", value)).strip().rstrip(" .")
    value = value.encode("utf-16-le")[:240].decode("utf-16-le", errors="ignore").rstrip(" .") or "_"
    if _DEVICE.fullmatch(value.split(".", 1)[0].rstrip()):
        value = "_" + value
    return value


def checked_path(path: Path) -> Path:
    """Validate existing ancestors without resolving through links."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("需要不含上级路径的绝对路径")
    if str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("不接受设备命名空间路径")
    if any(_INVALID.search(part) or part.endswith((" ", "."))
           or _DEVICE.fullmatch(part.split(".", 1)[0].rstrip()) for part in path.parts[1:]):
        raise ValueError("路径含 Windows 不支持的字符")
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError(f"不允许重解析点或链接：{part}")
    return path


def safe_join(root: Path, *parts: str) -> Path:
    root = checked_path(root)
    if any(part != safe_component(part) for part in parts):
        raise ValueError("不安全的路径组件")
    target = checked_path(root.joinpath(*parts))
    if not target.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise ValueError("路径超出指定根目录")
    return target


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        return cls(checked_path(root))

    @classmethod
    def default(cls) -> "AppPaths":
        location = os.environ.get("LOCALAPPDATA")
        if not location:
            raise ValueError("无法确定 Windows 应用数据目录")
        return cls.from_root(Path(location) / "LiveRecorderDesktop")

    @property
    def catalog(self) -> Path:
        return safe_join(self.root, "library.sqlite3")

    @property
    def platforms(self) -> Path:
        return safe_join(self.root, "platforms")

    def recording_root(self, path: Path) -> Path:
        media = checked_path(path).resolve(strict=False)
        app = checked_path(self.root).resolve(strict=False)
        if media.is_relative_to(app) or app.is_relative_to(media):
            raise ValueError("录制目录不能与应用数据目录相互包含")
        return media
