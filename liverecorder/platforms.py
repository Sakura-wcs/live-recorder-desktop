import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from http.cookies import SimpleCookie
from importlib import import_module
from pathlib import Path
from unicodedata import category
from urllib.parse import parse_qs, urlsplit

from .settings import PLATFORMS

NAMES = dict(zip(PLATFORMS, ("哔哩哔哩", "斗鱼", "虎牙", "抖音", "YouTube", "Twitch"), strict=True))
HOSTS = dict(zip(PLATFORMS, ("live.bilibili.com", "www.douyu.com", "www.huya.com",
                             "live.douyin.com", "www.youtube.com", "www.twitch.tv"), strict=True))
COOKIES = {
    "bilibili": ".bilibili.com", "douyu": ".douyu.com", "huya": ".huya.com",
    "douyin": ".douyin.com", "youtube": ".youtube.com",
}


class PlatformFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_NAME_NOTICES = {
    "missing": "平台未返回主播名称，可留空保存，监控时自动补齐。",
    "offline": "当前未开播，未获取到主播名称；可留空保存。",
    "timeout": "获取主播名称超时，可留空保存。",
    "network": "网络连接失败，可留空保存。",
    "rate_limit": "平台查询频率受限，可留空保存。",
    "access": "平台拒绝访问，请检查平台配置或网络；也可留空保存。",
    "identity": "平台返回的房间与输入不一致，可留空保存。",
    "platform": "暂时无法读取平台房间信息，可留空保存。",
}


@dataclass(frozen=True)
class NameLookup:
    name: str | None = None
    reason: str = "missing"

    @property
    def notice(self) -> str:
        return _NAME_NOTICES.get(self.reason, "")


def clean_author(value):
    if isinstance(value, str):
        value = value.strip()
        if value and not any(category(char) in {"Cc", "Cs"} for char in value):
            return value[:128].rstrip()
    return None


_DOUYIN_FRAME = re.compile(r'self\.__pace_f\.push\(\[\d+,("\w+:.+?")\]\)</script>')


def _douyin_name(response, url):
    if response.status_code != 200:
        return None
    requested, returned = urlsplit(url), urlsplit(response.url)
    if (returned.netloc, returned.path.rstrip("/")) != (requested.netloc, requested.path.rstrip("/")):
        return NameLookup(reason="identity")
    # The public room page can contain room state even when it has no streams.
    frames = [item for item in _DOUYIN_FRAME.findall(response.text) if "state" in item and "streamStore" in item]
    if not frames:
        return None
    try:
        states = json.loads(json.loads(frames[-1]).partition(":")[2])
        [state] = [item["state"] for item in states if isinstance(item, dict) and "state" in item]
        room = state["roomStore"]["roomInfo"]["room"]
        web_rid = room.get("web_rid")
        # `id_str` is an internal stream ID and need not match the public room URL.
        if web_rid not in (None, "") and str(web_rid) != requested.path.strip("/"):
            return NameLookup(reason="identity")
        owner = room.get("owner")
        name = clean_author(owner.get("nickname")) if isinstance(owner, dict) else None
        if name:
            return NameLookup(name, "found")
        return NameLookup(reason="offline" if room.get("status") == 4 else "missing")
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        return None


def _name_observer(session, url, platform):
    metadata = None

    def inspect(response, **_kwargs):
        nonlocal metadata
        metadata = _douyin_name(response, url) or metadata

    if platform == "douyin":
        session.http.hooks["response"].append(inspect)

    def result(plugin):
        if metadata is not None and (metadata.name or metadata.reason == "identity"):
            return metadata
        author = clean_author(plugin.get_author())
        return NameLookup(author, "found") if author else metadata or NameLookup()

    return result


class _SessionStream:
    def __init__(self, stream, http):
        self.stream, self.http = stream, http

    def read(self, size):
        from streamlink.exceptions import StreamError

        try:
            return self.stream.read(size)
        except StreamError:
            # The recorder already treats an input I/O fault as a disconnect
            # and attempts to salvage any data that has been written.
            raise OSError("直播流读取中断") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            # A transport teardown failure must not discard already-read
            # bytes by suppressing finalization in the recorder.
            with suppress(OSError):
                self.stream.close()
        finally:
            self.http.close()


def _platform_failure(error, *, for_name=False):
    from requests.exceptions import RequestException, Timeout, ConnectionError
    from streamlink.exceptions import FatalPluginError, StreamError

    seen = set()
    pending = [error]
    fallback = PlatformFailure("platform", "平台检测或取流失败，请检查平台状态与插件兼容性。")
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, RequestException):
            status = current.response.status_code if current.response is not None else None
            if status in {401, 403}:
                return PlatformFailure("access", "平台拒绝访问，请检查授权、平台配置或网络地区。")
            if status == 429:
                return PlatformFailure("rate_limit", "平台请求过于频繁，已暂停后重试。")
        if for_name and isinstance(current, Timeout):
            return PlatformFailure("timeout", "平台请求超时")
        if isinstance(current, (Timeout, ConnectionError)):
            return PlatformFailure("network", "平台连接失败或超时，请检查网络或代理。")
        if isinstance(current, FatalPluginError):
            fallback = PlatformFailure("access", "平台无法提供直播流，请检查授权与平台配置。")
        elif isinstance(current, StreamError):
            fallback = PlatformFailure("network", "直播流无法打开，请检查网络或媒体工具。")
        pending.extend(item for item in (current.__cause__, current.__context__, *current.args)
                       if isinstance(item, BaseException))
    return fallback


def normalize_room(platform: str, raw: str) -> str:
    if platform not in HOSTS:
        raise ValueError("未支持的平台")
    value = raw.strip()
    if "://" in value:
        parts = urlsplit(value)
        host = HOSTS[platform]
        accepted = {host, host.removeprefix("www.")}
        if platform == "youtube":
            accepted.add("youtu.be")
        if parts.scheme != "https" or parts.netloc.lower() not in accepted:
            raise ValueError("链接与所选平台不匹配")
        segments = parts.path.strip("/").split("/")
        if platform == "youtube":
            if parts.netloc.lower() == "youtu.be" and len(segments) == 1:
                value = "video:" + segments[0]
            elif segments == ["watch"]:
                value = "video:" + parse_qs(parts.query).get("v", [""])[0]
            elif segments[0] == "live" and len(segments) == 2:
                value = "video:" + segments[1]
            elif len(segments) in {2, 3} and segments[0] == "channel" and segments[2:] in ([], ["live"]):
                value = "channel:" + segments[1]
            else:
                raise ValueError("请输入 YouTube 频道 ID 或单场直播链接")
        elif platform == "douyin" and segments == [""]:
            ids = parse_qs(parts.query, keep_blank_values=True).get("live_web_rid", [])
            if len(ids) != 1:
                raise ValueError("抖音链接中找不到唯一的直播间数字 ID")
            value = ids[0]
        elif len(segments) == 1:
            value = segments[0]
        else:
            raise ValueError("请输入直播间链接，不能是子页面")
    if platform == "youtube":
        if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
            value = "channel:" + value
        if not re.fullmatch(r"(?:channel:UC[A-Za-z0-9_-]{22}|video:[A-Za-z0-9_-]{11})", value):
            raise ValueError("请输入 YouTube 频道 ID 或单场直播链接")
        return value
    pattern = r"[0-9]{1,25}" if platform in {"bilibili", "douyin"} else r"[A-Za-z0-9_-]{1,64}"
    if not re.fullmatch(pattern, value):
        raise ValueError("直播间 ID 格式不正确")
    return value.lower()


@dataclass(frozen=True)
class Adapter:
    platform: str

    @property
    def fields(self):
        return ("proxy", "cookie") if self.platform in COOKIES else ("proxy",)

    def url(self, identity: str) -> str:
        identity = normalize_room(self.platform, identity)
        host = HOSTS[self.platform]
        if self.platform == "youtube":
            kind, identifier = identity.split(":")
            return (f"https://{host}/channel/{identifier}/live" if kind == "channel"
                    else f"https://{host}/watch?v={identifier}")
        return f"https://{host}/{identity}"

    def plugin_class(self):
        return import_module(f"streamlink.plugins.{self.platform}").__plugin__

    def _session(self, settings, options):
        from streamlink import Streamlink

        # Plugins can log signed stream URLs; worker communicates fixed messages instead.
        logging.getLogger("streamlink").disabled = True
        logging.getLogger("streamlink").setLevel(logging.CRITICAL + 1)
        session = Streamlink(options, plugins_builtin=False)
        proxy = settings.get("proxy")
        if proxy:
            session.set_option("http-proxy", proxy)
        cookie = settings.get("cookie")
        if cookie and self.platform in COOKIES:
            jar = SimpleCookie()
            jar.load(cookie)
            for key, morsel in jar.items():
                session.http.cookies.set(key, morsel.value, domain=COOKIES[self.platform], path="/", secure=True)
        return session

    def lookup_name(self, identity: str, settings: dict) -> NameLookup:
        from requests.exceptions import RequestException
        from streamlink.exceptions import NoStreamsError, StreamlinkError

        url = self.url(identity)
        session = self._session(settings, {"http-timeout": 5})
        with session.http:
            result = _name_observer(session, url, self.platform)
            plugin = self.plugin_class()(session, url)
            try:
                initial = result(plugin)
                if initial.name:
                    return initial
                plugin.streams()
                return result(plugin)
            except NoStreamsError:
                return result(plugin)
            except (StreamlinkError, RequestException) as error:
                observed = result(plugin)
                return observed if observed.reason != "missing" else NameLookup(reason=_platform_failure(error, for_name=True).code)
            except (ValueError, TypeError, KeyError, AttributeError):
                observed = result(plugin)
                return observed if observed.reason != "missing" else NameLookup(reason="platform")

    def detect(self, identity: str, settings: dict, ffmpeg: Path, *, on_author=None):
        from requests.exceptions import RequestException
        from streamlink.exceptions import NoStreamsError, StreamlinkError
        from streamlink.stream.ffmpegmux import MuxedStream
        from streamlink.stream.hls import HLSStream
        from streamlink.stream.hls.hls import MuxedHLSStream

        session = self._session(settings, {"http-timeout": 12, "stream-timeout": 15,
                                          "stream-segment-timeout": 12, "ffmpeg-ffmpeg": str(ffmpeg),
                                          "ffmpeg-fout": "matroska"})
        keep_session = False
        try:
            url = self.url(identity)
            result = _name_observer(session, url, self.platform) if on_author is not None else None
            plugin = self.plugin_class()(session, url)
            try:
                streams = plugin.streams()
            except NoStreamsError:
                streams = None
            finally:
                if result is not None and (author := result(plugin).name):
                    on_author(author)
            if not streams:
                return None
            stream = streams.get("best")
            if stream is None:
                raise PlatformFailure("unsupported", "平台返回了直播流，但没有可选的录制质量。")
            if self.platform == "youtube" and not isinstance(stream, (HLSStream, MuxedHLSStream)):
                hls = [item for name, item in streams.items() if name not in {"best", "worst"}
                       and isinstance(item, (HLSStream, MuxedHLSStream))]
                if not hls:
                    raise PlatformFailure("unsupported", "YouTube 未提供本版本支持的 HLS 直播流。")
                stream = hls[-1]
            extension = (".mkv" if isinstance(stream, MuxedStream) else ".ts" if isinstance(stream, HLSStream)
                         else ".flv" if self.platform in {"bilibili", "douyu", "huya", "douyin"} else ".mp4")
            title = plugin.title or identity
            opened = _SessionStream(stream.open(), session.http)
            keep_session = True
            return opened, extension, title
        except NoStreamsError:
            return None
        except (StreamlinkError, RequestException) as error:
            raise _platform_failure(error) from None
        finally:
            if not keep_session:
                session.http.close()


def adapter_for(platform: str) -> Adapter:
    if platform not in PLATFORMS:
        raise ValueError("未支持的平台")
    return Adapter(platform)
