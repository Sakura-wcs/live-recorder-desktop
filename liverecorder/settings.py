import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .paths import checked_path, safe_join

PLATFORMS = ("bilibili", "douyu", "huya", "douyin", "youtube", "twitch")


class CredentialStore(Protocol):
    def write(self, name: str, value: str) -> None: ...
    def read(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


class WindowsCredentials:
    def __init__(self):
        import pywintypes
        import win32cred

        self.api = win32cred
        self.error = pywintypes.error

    def write(self, name: str, value: str) -> None:
        self.api.CredWrite({
            "Type": self.api.CRED_TYPE_GENERIC,
            "TargetName": name,
            "CredentialBlob": value.encode("utf-8"),
            "Persist": self.api.CRED_PERSIST_LOCAL_MACHINE,
        }, 0)

    def read(self, name: str) -> str | None:
        try:
            item = self.api.CredRead(name, self.api.CRED_TYPE_GENERIC, 0)
        except self.error as error:
            if error.winerror != 1168:
                raise
            return None
        return item["CredentialBlob"].decode("utf-8")

    def delete(self, name: str) -> None:
        try:
            self.api.CredDelete(name, self.api.CRED_TYPE_GENERIC, 0)
        except self.error as error:
            if error.winerror != 1168:
                raise


class PlatformSettings:
    """Persist only allowlisted fields; sensitive values live in a credential store."""

    def __init__(self, directory: Path, credentials: CredentialStore):
        self.directory = checked_path(directory)
        self.credentials = credentials

    def _location(self, platform: str) -> Path:
        if platform not in PLATFORMS:
            raise ValueError("未知直播平台")
        return safe_join(self.directory, platform + ".json")

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "values": {}, "credentials": {}}
        if not isinstance(result, dict) or result.get("version") != 1:
            raise ValueError("不支持的平台配置版本")
        for section in ("values", "credentials"):
            if not isinstance(result.get(section), dict):
                raise ValueError("平台配置格式错误")
        return result

    @staticmethod
    def _valid_ref(platform: str, key: str, ref: str) -> bool:
        prefix = f"LiveRecorderDesktop/{platform}/{key}/"
        suffix = ref.removeprefix(prefix)
        return (key in {"cookie", "proxy"} and ref.startswith(prefix) and len(suffix) == 32
                and all(c in "0123456789abcdef" for c in suffix))

    def load(self, platform: str) -> dict:
        data = self._read(self._location(platform))
        result = dict(data["values"])
        for key, ref in data["credentials"].items():
            if not self._valid_ref(platform, key, ref):
                raise ValueError("无效的凭据引用")
            value = self.credentials.read(ref)
            if value is None:
                raise ValueError("平台凭据已失效，请重新配置")
            result[key] = value
        return result

    def save(self, platform: str, values: dict) -> None:
        path = self._location(platform)
        if set(values) - {"cookie", "proxy"}:
            raise ValueError("平台配置含不支持的字段")
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            raise ValueError("配置字段必须是文本")
        secret = {}
        plain = {}
        for key, value in values.items():
            if not value:
                continue
            if key == "cookie":
                secret[key] = value
                continue
            try:
                proxy = urlsplit(value)
                port = proxy.port
            except ValueError:
                raise ValueError("代理地址格式不正确") from None
            if (proxy.scheme not in {"http", "https", "socks5", "socks5h"} or not proxy.hostname
                    or port is None or proxy.path not in {"", "/"} or proxy.query or proxy.fragment):
                raise ValueError("代理地址格式不正确")
            (secret if proxy.username is not None or proxy.password is not None else plain)[key] = value
        previous = self._read(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        refs = {}
        temporary = None
        try:
            for key, value in secret.items():
                ref = f"LiveRecorderDesktop/{platform}/{key}/{uuid4().hex}"
                self.credentials.write(ref, value)
                refs[key] = ref
            with NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                    dir=checked_path(path.parent), suffix=".tmp") as file:
                temporary = Path(file.name)
                json.dump({"version": 1, "values": plain, "credentials": refs},
                          file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(checked_path(temporary), checked_path(path))
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            for ref in refs.values():
                self.credentials.delete(ref)
            raise
        for key, ref in previous["credentials"].items():
            if self._valid_ref(platform, key, ref):
                self.credentials.delete(ref)
