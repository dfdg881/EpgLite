#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import os
import re
import shutil
import ssl
import sys
import zlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from time import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit, urlparse
from urllib.request import Request, ProxyHandler, build_opener, HTTPSHandler

# ---------------------------------------------------------------------------
# 可选依赖适配（全部有标准库降级方案）
# ---------------------------------------------------------------------------
try:
    from opencc import OpenCC
    _OPENCC_T2S = OpenCC("t2s")
except ImportError:  # 未安装 opencc 时，标题原样保留
    class _NoOpConverter:
        def convert(self, text):
            return text
    _OPENCC_T2S = _NoOpConverter()

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # 未安装 certifi 时使用系统默认 CA
    _SSL_CONTEXT = ssl.create_default_context()

try:
    from zoneinfo import ZoneInfo

    def _load_timezone(name):
        return ZoneInfo(name)
except ImportError:  # Python < 3.9，尝试 pytz
    try:
        import pytz

        def _load_timezone(name):
            return pytz.timezone(name)
    except ImportError:
        _load_timezone = None

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Accept-Language": "zh-CN,zh;q=0.8",
    "User-Agent": DEFAULT_USER_AGENT,
}

# ---------------------------------------------------------------------------
# 常量（与 iptv-api 一致）
# ---------------------------------------------------------------------------
SUBSCRIBE_EPG_MAX_SOURCES = 3          # 附加(发现)EPG 源最多并入的数量
EPG_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024        # 压缩后最大 64MB
EPG_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024   # 解压后最大 256MB
EPG_MAX_PROGRAMMES = 500000             # 单个源最多节目条数
EPG_DAYS_BACK = 1                       # 保留过去 N 天
EPG_DAYS_AHEAD = 14                     # 保留未来 N 天
MAX_RETRIES = 2                         # 每个源重试次数

# 名称规范化：需要去除的冗余字符/后缀
SUB_PATTERN = re.compile(
    r"-|_|\((.*?)\)|（(.*?)）|\[(.*?)]|「(.*?)」| |｜|频道|普清|标清|高清|HD|hd|超清|超高|超高清|4K|4k|中央|央视|电视台|台|电信|联通|移动"
)
REPLACE_DICT = {
    "plus": "+",
    "PLUS": "+",
    "＋": "+",
}

URL_PATTERN = re.compile(
    r"((https?|rtmp|rtsp)://)?([^:@/]+(:[^:@/]*)?@)?(\[[0-9a-fA-F:]+]|([\w-]+\.)+[\w-]+)\S*"
)


# ---------------------------------------------------------------------------
# 别名 / 名称规范化（对应 utils/alias.py、utils/tools.py:format_name、
#                         utils/channel.py:format_channel_name）
# ---------------------------------------------------------------------------
class Alias:
    """读取 alias.txt：主名,别名1,别名2,...；支持 re: 开头的正则别名。"""

    def __init__(self, path=None):
        self.primary_to_aliases = {}
        self.alias_to_primary = {}
        self.pattern_to_primary = []
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip() and not line.strip().startswith("#") and "," in line:
                        parts = [p.strip() for p in line.split(",")]
                        primary = parts[0]
                        aliases = set(parts[1:])
                        aliases.add(format_name(primary))
                        self.primary_to_aliases[primary] = aliases
                        for alias in aliases:
                            self.alias_to_primary[alias] = primary
                            if alias.startswith("re:"):
                                raw_pattern = alias[3:]
                                try:
                                    pattern = re.compile(raw_pattern)
                                    if (pattern, primary) not in self.pattern_to_primary:
                                        self.pattern_to_primary.append((pattern, primary))
                                except re.error:
                                    pass
                        self.alias_to_primary[primary] = primary

    def get(self, name):
        return self.primary_to_aliases.get(name, set())

    def get_primary(self, name):
        primary_name = self.alias_to_primary.get(name, None) or self.get_primary_by_pattern(name)
        if primary_name is None:
            alias_format_name = format_name(name)
            primary_name = self.alias_to_primary.get(alias_format_name, name)
        return primary_name

    def get_primary_by_pattern(self, name):
        for pattern, primary in self.pattern_to_primary:
            if pattern.search(name):
                return primary
        return None


def format_name(name):
    """名称规范化：繁体转简体 + 去除冗余后缀 + plus 归一 + 小写。"""
    name = _OPENCC_T2S.convert(name)
    name = SUB_PATTERN.sub("", name)
    for old, new in REPLACE_DICT.items():
        name = name.replace(old, new)
    return name.lower()


_channel_alias = None


def init_alias(path=None):
    global _channel_alias
    _channel_alias = Alias(path)


def format_channel_name(name):
    """频道名统一归一：先用别名表解析（精确/正则/规范化兜底），返回主名。"""
    return _channel_alias.get_primary(name)


RESOLUTION_MARKERS = ("4k", "8k", "uhd", "超清", "超高清", "hdr", "杜比", "dolby")


def _has_resolution_marker(name):
    """判断频道名是否带分辨率标记（4K/8K/UHD/超清/杜比 等）。"""
    low = (name or "").lower()
    return any(marker in low for marker in RESOLUTION_MARKERS)


class ChannelMatcher:
    """频道匹配器：demo.txt 中的每个频道名都是各自独立的目标频道。

    匹配流程（两轮，前一轮命中即不再进入后一轮）：
      1. 直接匹配：先按原名精确匹配，再按规范化名称（去除 高清/HD/4K/括号/分隔符
         等冗余，见 format_name）匹配；同一规范化名对应多个目标时，优先取
         “分辨率标记一致”的目标（显示名带 4K/8K/UHD/超清 等标记则优先
         匹配带同标记的目标，如 “CCTV16 4K” 归 “CCTV16-4K”），
         再按名称最长包含于 EPG 频道名的目标（如 “北京卫视高清” 归 “北京卫视”）。
      2. 别名匹配：仍未匹配的 EPG 频道，通过 alias.txt 别名表解析
         （精确别名 / re: 正则别名 / 规范化兜底），映射回目标频道。

    匹配结果始终使用目标频道在 demo.txt 中的原始名称作为键，
    不会因别名归一而把多个目标频道合并成一个。
    """

    def __init__(self, names, alias):
        self.alias = alias
        self.targets = []
        seen = set()
        for name in (names or []):
            name = (name or "").strip()
            if name and name not in seen:
                seen.add(name)
                self.targets.append(name)
        self.target_set = set(self.targets)
        self.norm_to_targets = defaultdict(list)
        for target in self.targets:
            self.norm_to_targets[format_name(target)].append(target)

    def resolve(self, display_name):
        d = (display_name or "").strip()
        if not d:
            return None
        # 第一轮-1：原名精确匹配
        if d in self.target_set:
            return d
        # 第一轮-2：规范化名称匹配（去冗余后缀）
        candidates = self.norm_to_targets.get(format_name(d))
        if candidates:
            if len(candidates) == 1:
                return candidates[0]
            # 多个目标规范化后同名（如 北京卫视/北京卫视4K、CCTV16/CCTV16-4K）：
            # 先按分辨率标记一致性（4K/8K/UHD/超清 等）分流，避免 4K 频道被并入普清频道；
            # 再取名称最长包含于 EPG 频道名的目标
            lower_d = d.lower()
            display_res = _has_resolution_marker(d)
            best, best_score = None, None
            for idx, target in enumerate(candidates):
                sub_len = len(target) if target and target.lower() in lower_d else 0
                marker_agree = _has_resolution_marker(target) == display_res
                score = (marker_agree, sub_len, -idx)
                if best is None or score > best_score:
                    best, best_score = target, score
            return best
        # 第二轮：通过别名表解析（精确别名 / re: 正则 / 规范化兜底）
        primary = self.alias.get_primary(d)
        if primary in self.target_set:
            return primary
        return None


# ---------------------------------------------------------------------------
# XMLTV 流式解析（对应 updates/epg/request.py 的 EpgStreamParser/parse_epg）
# ---------------------------------------------------------------------------
class EpgResourceLimitError(ValueError):
    pass


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag


def _child_text(element, name):
    for child in element:
        if _local_name(child.tag) == name:
            return child.text or ""
    return ""


class EpgStreamParser:
    def __init__(
        self,
        matcher=None,
        include_unmatched=True,
        max_programmes=None,
        days_back=None,
        days_ahead=None,
    ):
        self.parser = ET.XMLPullParser(events=("end",))
        self.matcher = matcher
        self.include_unmatched = include_unmatched
        self.max_programmes = max_programmes
        self.days_back = days_back
        self.days_ahead = days_ahead
        self.channels = {}
        self.included_channel_ids = set()
        self.programmes = defaultdict(list)
        self.programme_count = 0
        self.retained_programme_count = 0
        self.now_by_timezone = {}

    def feed(self, content):
        self.parser.feed(content)
        self._drain()

    def close(self):
        self.parser.close()
        self._drain()
        return self.channels, self.programmes

    def _drain(self):
        for _, element in self.parser.read_events():
            tag = _local_name(element.tag)
            if tag == "channel":
                self._process_channel(element)
                element.clear()
            elif tag == "programme":
                self._process_programme(element)
                element.clear()

    def _process_channel(self, element):
        channel_id = element.get("id")
        display_name = _child_text(element, "display-name").strip()
        if not channel_id or not display_name:
            return
        if self.matcher and self.matcher.targets:
            key = self.matcher.resolve(display_name)
        else:  # 未提供目标频道时不做过滤
            key = display_name
        if key is None:
            if not self.include_unmatched:
                return
            key = display_name
        # 匹配结果保留目标频道原名（或未匹配时的 EPG 原名），不做别名合并
        self.channels[channel_id] = key
        self.included_channel_ids.add(channel_id)

    def _process_programme(self, element):
        self.programme_count += 1
        if self.max_programmes and self.programme_count > self.max_programmes:
            raise EpgResourceLimitError(
                f"EPG programme count exceeds {self.max_programmes}"
            )

        channel_id = element.get("channel")
        if channel_id not in self.included_channel_ids:
            return
        try:
            channel_start = datetime.strptime(
                re.sub(r"\s+", "", element.get("start") or ""),
                "%Y%m%d%H%M%S%z",
            )
            channel_stop = datetime.strptime(
                re.sub(r"\s+", "", element.get("stop") or ""),
                "%Y%m%d%H%M%S%z",
            )
        except (TypeError, ValueError):
            return

        timezone = channel_start.tzinfo
        if timezone not in self.now_by_timezone:
            self.now_by_timezone[timezone] = (
                datetime.now(timezone) if timezone else datetime.now()
            )
        now = self.now_by_timezone[timezone]
        if self.days_back is not None and channel_stop < now - timedelta(days=self.days_back):
            return
        if self.days_ahead is not None and channel_start > now + timedelta(days=self.days_ahead):
            return

        title = _child_text(element, "title").strip()
        if not title:
            return
        output = ET.Element(
            "programme",
            attrib={
                "channel": channel_id,
                "start": channel_start.strftime("%Y%m%d%H%M%S %z"),
                "stop": channel_stop.strftime("%Y%m%d%H%M%S %z"),
            },
        )
        ET.SubElement(output, "title", attrib={"lang": "zh"}).text = _OPENCC_T2S.convert(title)
        self.programmes[channel_id].append(output)
        self.retained_programme_count += 1


def parse_epg(
    epg_content,
    matcher=None,
    include_unmatched=True,
    max_programmes=None,
    days_back=None,
    days_ahead=None,
):
    parser = EpgStreamParser(
        matcher=matcher,
        include_unmatched=include_unmatched,
        max_programmes=max_programmes,
        days_back=days_back,
        days_ahead=days_ahead,
    )
    try:
        if isinstance(epg_content, str):
            epg_content = epg_content.encode("utf-8")
        elif isinstance(epg_content, bytearray):
            epg_content = bytes(epg_content)
        if isinstance(epg_content, bytes) and epg_content.startswith(b"\x1f\x8b"):
            epg_content = zlib.decompress(epg_content, 16 + zlib.MAX_WBITS)
        parser.feed(epg_content)
        return parser.close()
    except (ET.ParseError, EpgResourceLimitError) as exc:
        print(f"[EPG] XML 解析失败，已跳过该源: {exc}")
        return {}, defaultdict(list)


# ---------------------------------------------------------------------------
# URL 与订阅条目处理（对应 utils/tools.py 的 github_blob_to_raw、
#                          get_subscribe_entries 与 request.py 的去重逻辑）
# ---------------------------------------------------------------------------
def github_blob_to_raw(url):
    """GitHub blob/tree 页面地址转为 raw.githubusercontent.com 直链。"""
    if not url:
        return url
    if "raw.githubusercontent.com" in url:
        return url
    parsed = urlparse(url)
    netloc = parsed.netloc or ""
    if "github.com" not in netloc:
        return url
    path = (parsed.path or "").lstrip("/")
    parts = path.split("/")
    if len(parts) < 5:
        return url
    owner, repo, marker = parts[0], parts[1], parts[2]
    if marker not in ("blob", "tree"):
        return url
    branch = parts[3]
    file_parts = parts[4:]
    try:
        decoded_path = "/".join(unquote(p) for p in file_parts)
        safe_path = quote(decoded_path, safe="/")
    except Exception:
        safe_path = "/".join(file_parts)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{safe_path}"


def get_request_url_candidates(url):
    """github 页面地址转换为候选直链，其余地址原样返回。"""
    raw_url = github_blob_to_raw(url)
    return [raw_url]


def _canonical_epg_url(url):
    raw_url = github_blob_to_raw(str(url or "").strip())
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return raw_url
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
        port = parsed.port
        if port and not (
            parsed.scheme.lower() == "http" and port == 80
            or parsed.scheme.lower() == "https" and port == 443
        ):
            host = f"{host}:{port}"
        return urlunsplit((
            parsed.scheme.lower(),
            f"{userinfo}{host}",
            parsed.path or "/",
            parsed.query,
            "",
        ))
    except (TypeError, ValueError):
        return raw_url


def _entry_key(entry):
    url = entry.get("url") if isinstance(entry, dict) else entry
    headers = entry.get("headers") if isinstance(entry, dict) else None
    header_key = tuple(
        sorted((str(key).lower(), str(value)) for key, value in (headers or {}).items())
    )
    return _canonical_epg_url(url), header_key


def dedupe_epg_entries(whitelist_entries, default_entries, discovered_entries, discovered_limit):
    """白名单 > 配置源 > 附加源 的优先级去重合并。"""
    result = []
    seen = set()
    duplicate_count = 0
    limited_count = 0
    discovered_added = 0
    groups = (
        (whitelist_entries, 0, "whitelist"),
        (default_entries, 1, "configured"),
        (discovered_entries, 2, "discovered"),
    )
    for entries, priority, origin in groups:
        for raw_entry in entries or ():
            if origin == "discovered" and discovered_added >= discovered_limit:
                limited_count += 1
                continue
            entry = dict(raw_entry) if isinstance(raw_entry, dict) else {"url": raw_entry}
            key = _entry_key(entry)
            if not key[0] or key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            entry["_priority"] = priority
            entry["_origin"] = origin
            result.append(entry)
            if origin == "discovered":
                discovered_added += 1
    return result, duplicate_count, limited_count, discovered_added


def parse_epg_entries(path):
    """解析 EPG 订阅文件，返回 (白名单清单, 普通清单)。

    支持 [WHITELIST] 区块、行内 Header=Value（UA 自动转为 User-Agent）、
    # 开头的注释/禁用行。条目格式: {"url":..., "headers":{...}}
    """
    inside, outside = [], []
    if not os.path.exists(path):
        return inside, outside
    header_re = re.compile(r"^\[.*]$")
    in_section = False
    kv_re = re.compile(r"(?P<k>\w+)=((?P<q>\".*?\"|'.*?')|(?P<v>\S+))")
    seen_inside, seen_outside = set(), set()

    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            s = line.strip()
            if not s:
                continue
            if header_re.match(s):
                in_section = s.upper() == "[WHITELIST]"
                continue
            if s.startswith("#"):
                continue
            match = URL_PATTERN.search(s)
            if not match:
                continue
            url = match.group().strip()
            remainder = s[match.end():].strip()
            headers = {}
            for m in kv_re.finditer(remainder):
                key = m.group("k")
                val = m.group("q") or m.group("v")
                if not val:
                    continue
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                if key.lower() in ("ua", "useragent", "user-agent"):
                    headers["User-Agent"] = val
                else:
                    headers[key] = val
            entry = {"url": url}
            if headers:
                entry["headers"] = headers
            target = inside if in_section else outside
            seen = seen_inside if in_section else seen_outside
            dedupe_key = (url, tuple(sorted(headers.items())))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            target.append(entry)
    return inside, outside


def count_disabled_epg_urls(path):
    """统计订阅文件中被注释（# 开头且含 URL）的禁用源数量。"""
    if not os.path.exists(path):
        return 0
    disabled_count = 0
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line.startswith("#"):
                continue
            commented = line.lstrip("#").strip()
            if commented and URL_PATTERN.match(commented):
                disabled_count += 1
    return disabled_count


def disable_epg_urls_in_file(path, urls):
    """将指定 url 的行注释掉（#），返回 (被禁用数, 剩余启用数)。"""
    target_urls = {url.strip() for url in urls if url and str(url).strip()}
    if not target_urls or not os.path.exists(path):
        return 0, 0
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    disabled = 0
    active = 0
    for i, original in enumerate(lines):
        stripped = original.strip()
        if not stripped or stripped.startswith("#") or re.match(r"^\[.*]$", stripped):
            continue
        match = URL_PATTERN.search(stripped)
        if not match:
            continue
        url = match.group().strip()
        if url in target_urls:
            if not original.lstrip().startswith("#"):
                lines[i] = "#" + original
                disabled += 1
        else:
            active += 1
    if disabled:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    return disabled, active


# ---------------------------------------------------------------------------
# 抓取（对应 update/epg/request.py 的 _consume_epg_response / _fetch_epg，
#       使用标准库 urllib 替代 aiohttp）
# ---------------------------------------------------------------------------
def _consume_epg_response(response, matcher, include_unmatched, limits):
    parser = EpgStreamParser(
        matcher=matcher,
        include_unmatched=include_unmatched,
        max_programmes=limits["max_programmes"],
        days_back=limits["days_back"],
        days_ahead=limits["days_ahead"],
    )
    compressed_size = 0
    decompressed_size = 0
    digest = hashlib.sha256()
    decompressor = None
    encoding = (response.headers.get("Content-Encoding") or "").lower()
    undecided = encoding not in {"gzip", "x-gzip", "deflate"}
    prefix = b""

    def feed(content):
        nonlocal decompressed_size
        if not content:
            return
        decompressed_size += len(content)
        if decompressed_size > EPG_MAX_DECOMPRESSED_BYTES:
            raise EpgResourceLimitError(
                f"EPG decompressed content exceeds {EPG_MAX_DECOMPRESSED_BYTES} bytes"
            )
        digest.update(content)
        parser.feed(content)

    if encoding in {"gzip", "x-gzip"}:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()

    start = time()
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        compressed_size += len(chunk)
        if compressed_size > EPG_MAX_DOWNLOAD_BYTES:
            raise EpgResourceLimitError(
                f"EPG response exceeds {EPG_MAX_DOWNLOAD_BYTES} bytes"
            )
        if limits["total_timeout"] and time() - start > limits["total_timeout"]:
            raise TimeoutError(f"EPG fetch timed out: {limits['total_timeout']}s")
        if undecided:
            prefix += chunk
            if len(prefix) < 2:
                continue
            undecided = False
            if prefix.startswith(b"\x1f\x8b"):
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            chunk = prefix
            prefix = b""
        if decompressor:
            pending = chunk
            while pending:
                remaining = EPG_MAX_DECOMPRESSED_BYTES - decompressed_size
                if remaining <= 0:
                    raise EpgResourceLimitError(
                        f"EPG decompressed content exceeds {EPG_MAX_DECOMPRESSED_BYTES} bytes"
                    )
                feed(decompressor.decompress(pending, remaining + 1))
                pending = decompressor.unconsumed_tail
        else:
            feed(chunk)

    if undecided and prefix:
        feed(prefix)
    if decompressor:
        remaining = EPG_MAX_DECOMPRESSED_BYTES - decompressed_size
        if remaining <= 0:
            raise EpgResourceLimitError(
                f"EPG decompressed content exceeds {EPG_MAX_DECOMPRESSED_BYTES} bytes"
            )
        feed(decompressor.flush(remaining + 1))
    if decompressed_size == 0:
        raise ValueError("Empty EPG response")
    channels, programmes = parser.close()
    return channels, programmes, digest.hexdigest(), {
        "downloaded_bytes": compressed_size,
        "decompressed_bytes": decompressed_size,
        "programmes": parser.programme_count,
        "retained_programmes": parser.retained_programme_count,
    }


def _fetch_epg(url, headers, proxy, timeout, matcher, include_unmatched, limits):
    """抓取单个 EPG 源，返回 (channels, programmes, content_hash, stats)。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        for candidate in get_request_url_candidates(url):
            try:
                request = Request(candidate, headers=headers)
                handlers = [HTTPSHandler(context=_SSL_CONTEXT)]
                if proxy:
                    handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
                opener = build_opener(*handlers)
                with opener.open(request, timeout=timeout) as response:
                    return _consume_epg_response(
                        response, matcher, include_unmatched, limits
                    )
            except KeyboardInterrupt:
                raise
            except EpgResourceLimitError:
                raise
            except Exception as exc:
                last_error = exc
        if attempt < MAX_RETRIES - 1:
            print(f"[EPG] 源 {url} 获取失败，正在重试({attempt + 1}/{MAX_RETRIES - 1})...")
    raise Exception(f"请求失败，已达最大重试次数: {url}") from last_error


# ---------------------------------------------------------------------------
# 主流程（对应 updates/epg/request.py 的 get_epg）
# ---------------------------------------------------------------------------
def get_epg(
    names,
    epg_path="config/epg.txt",
    extra_entries=None,
    include_unmatched=False,
    max_programmes=EPG_MAX_PROGRAMMES,
    days_back=EPG_DAYS_BACK,
    days_ahead=EPG_DAYS_AHEAD,
    fetch_concurrency=4,
    request_timeout=10,
    proxy=None,
    auto_disable=False,
    user_agent=None,
    verbose=False,
):
    """
    抓取并合并多个 EPG 源，自动匹配频道。

    匹配流程：demo.txt 中的频道名为各自独立的目标频道——先直接匹配
    （原名精确 / 规范化名称），未匹配到的再尝试 alias.txt 别名解析，
    匹配结果保留目标频道原名（不会因别名把多个频道合并成一个）。

    :param names: 期望的频道名称列表（demo.txt/频道列表中的名称）。
    :return: {目标频道名: [ET.Element(programme), ...]}，
             每个频道的节目按开始时间正序排列。
    """
    global DEFAULT_HEADERS
    if user_agent:
        DEFAULT_HEADERS = {**DEFAULT_HEADERS, "User-Agent": user_agent}

    matcher = ChannelMatcher(names, _channel_alias)
    whitelist_entries, default_entries = parse_epg_entries(epg_path)
    entries, duplicate_count, limited_count, discovered_count = dedupe_epg_entries(
        whitelist_entries, default_entries, extra_entries or (), SUBSCRIBE_EPG_MAX_SOURCES
    )
    disabled_count = count_disabled_epg_urls(epg_path)
    print(
        f"[EPG] 配置源 {len(default_entries)} 个、白名单 {len(whitelist_entries)} 个、"
        f"禁用 {disabled_count} 个、附加源 {discovered_count} 个、"
        f"去重 {duplicate_count} 个、超限 {limited_count} 个，共抓取 {len(entries)} 个源"
    )
    if not entries:
        print("[EPG] 没有可用的 EPG 源，请检查订阅文件")
        return {}

    start_time = time()
    limits = {
        "max_programmes": max_programmes,
        "days_back": days_back,
        "days_ahead": days_ahead,
        "total_timeout": max(60, request_timeout * 6),
    }
    headers = dict(DEFAULT_HEADERS)

    seen_content_hashes = set()
    programme_records = defaultdict(dict)
    completed_sources = 0
    disabled_urls = set()

    def process_entry(entry):
        request_url = entry.get("url")
        source_url = entry.get("source_url", request_url)
        merged_headers = dict(headers)
        entry_headers = entry.get("headers") or {}
        merged_headers.update({k: v for k, v in entry_headers.items() if v is not None})
        try:
            channels, programmes, content_hash, stats = _fetch_epg(
                request_url,
                merged_headers,
                proxy,
                request_timeout,
                matcher,
                include_unmatched,
                limits,
            )
            return {
                "request_url": request_url,
                "source_url": source_url,
                "priority": entry.get("_priority", 2),
                "channels": channels,
                "programmes": programmes,
                "content_hash": content_hash,
                "stats": stats,
            }
        except Exception as exc:
            return {
                "request_url": request_url,
                "source_url": source_url,
                "priority": entry.get("_priority", 2),
                "error": exc,
            }

    with ThreadPoolExecutor(max_workers=max(1, fetch_concurrency)) as executor:
        futures = {executor.submit(process_entry, entry): entry for entry in entries}
        for future in as_completed(futures):
            result = future.result()
            completed_sources += 1
            elapsed = time() - start_time
            remaining = max(0, len(entries) - completed_sources)
            print(
                f"[EPG] 进度 {completed_sources}/{len(entries)}，剩余 {remaining} 个源，"
                f"已用时 {elapsed:.0f}s"
            )
            if "error" in result:
                if verbose:
                    print(f"      源 {result['request_url']} 处理失败: {result['error']}")
                continue
            if result["content_hash"] in seen_content_hashes:
                if verbose:
                    print(f"      源 {result['request_url']} 内容与已抓取源重复，跳过")
                continue
            seen_content_hashes.add(result["content_hash"])
            priority = result["priority"]
            # channels 字典：channel_id -> 目标频道原名（匹配结果，未做别名合并）
            for channel_id, key in result["channels"].items():
                for programme in result["programmes"].get(channel_id, ()):
                    programme_key = (programme.get("start"), programme.get("stop"))
                    existing = programme_records[key].get(programme_key)
                    if existing is None or priority < existing[0]:
                        programme_records[key][programme_key] = (priority, programme)
            if verbose:
                print(
                    f"      源 {result['request_url']} 解析完成：保留节目 "
                    f"{result['stats']['retained_programmes']} 条"
                )
            if not result["channels"]:
                if auto_disable:
                    disabled_urls.add(result["source_url"])
                    print(
                        f"[EPG] 源 {result['source_url']} 未匹配任何频道，已标记禁用"
                    )

    active_count = len(whitelist_entries) + len(default_entries)
    if auto_disable and disabled_urls:
        disabled_now, active_count = disable_epg_urls_in_file(epg_path, disabled_urls)
    print(
        f"[EPG] 抓取完成：匹配到 {len(programme_records)} 个频道的节目，"
        f"已用 {time() - start_time:.0f}s"
    )

    result = {
        channel_name: [
            record[1]
            for _, record in sorted(records.items(), key=lambda item: item[0])
        ]
        for channel_name, records in programme_records.items()
    }
    return result


# ---------------------------------------------------------------------------
# 输出（对应 updates/epg/tools.py 的 write_to_xml / compress_to_gz）
# ---------------------------------------------------------------------------
def write_to_xml(programmes, path, timezone="Asia/Shanghai"):
    if _load_timezone is not None:
        tz = _load_timezone(timezone)
    else:  # 全部不可用时退化为固定 +08:00
        from datetime import timezone as _tz
        tz = _tz(timedelta(hours=8))
    root = ET.Element("tv", attrib={"date": datetime.now(tz).strftime("%Y%m%d%H%M%S %z")})
    for channel_id, data in programmes.items():
        channel_elem = ET.SubElement(root, "channel", attrib={"id": channel_id})
        display_name_elem = ET.SubElement(channel_elem, "display-name", attrib={"lang": "zh"})
        display_name_elem.text = channel_id
        for prog in data:
            prog.set("channel", channel_id)
            root.append(prog)
    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="\t")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def compress_to_gz(input_path, output_path):
    with open(input_path, "rb") as f_in:
        with gzip.open(output_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)


# ---------------------------------------------------------------------------
# 频道列表读取：支持 iptv-api 的 txt/demo 格式（含 #genre# 分类行）、
#              纯每行一个频道名的 txt、以及 m3u 播放列表格式
# ---------------------------------------------------------------------------
def load_channel_names(path):
    names = []
    if not os.path.exists(path):
        print(f"[EPG] 频道列表文件不存在: {path}")
        return names
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.splitlines()
    if any(line.strip().startswith("#EXTM3U") for line in lines):
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#EXTINF"):
                continue
            parts = re.split(r"[，,]", stripped, maxsplit=3)
            name = parts[-1].strip() if len(parts) > 1 else ""
            name = re.sub(r"\s*$", "", name)
            if name and name not in names:
                names.append(name)
        return names
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#genre#" in stripped:
            continue  # 分类行，如 “📺央视频道,#genre#”
        name = re.split(r"[，,]", stripped, maxsplit=1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="独立版 EPG 获取工具（提取自 iptv-api）：抓取 XMLTV 节目单，"
                    "按别名/规范化名称自动匹配频道并输出 epg.xml / epg.gz",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--channels", default="config/demo.txt",
                        help="频道列表文件（txt/demo 格式或 m3u），默认 config/demo.txt")
    parser.add_argument("--epg-sources", default="config/epg.txt",
                        help="EPG 订阅源文件，默认 config/epg.txt")
    parser.add_argument("--alias", default="config/alias.txt",
                        help="别名表文件，默认 config/alias.txt")
    parser.add_argument("--extra-epg-url", action="append", default=[],
                        help="额外附加的 EPG 源 URL（可多次指定）")
    parser.add_argument("--output-xml", default="output/epg.xml",
                        help="EPG XML 输出路径，默认 output/epg.xml")
    parser.add_argument("--output-gz", default="output/epg.gz",
                        help="EPG gz 输出路径，默认 output/epg.gz")
    parser.add_argument("--no-gz", action="store_true", help="不生成 gzip 压缩版")
    parser.add_argument("--include-unmatched", action="store_true",
                        help="保留未匹配到需求频道的 EPG 频道（默认过滤）")
    parser.add_argument("--days-back", type=int, default=EPG_DAYS_BACK,
                        help=f"保留过去 N 天节目，默认 {EPG_DAYS_BACK}")
    parser.add_argument("--days-ahead", type=int, default=EPG_DAYS_AHEAD,
                        help=f"保留未来 N 天节目，默认 {EPG_DAYS_AHEAD}")
    parser.add_argument("--max-programmes", type=int, default=EPG_MAX_PROGRAMMES,
                        help=f"单源最大节目条数上限，默认 {EPG_MAX_PROGRAMMES}")
    parser.add_argument("--fetch-concurrency", type=int, default=4,
                        help="并发抓取源数，默认 4")
    parser.add_argument("--request-timeout", type=int, default=20,
                        help="请求超时秒数（总超时=其 6 倍且不小于 60s），默认 20")
    parser.add_argument("--proxy", default=None, help="HTTP 代理，如 http://127.0.0.1:7890")
    parser.add_argument("--timezone", default="Asia/Shanghai",
                        help="生成文件的时间戳时区，默认 Asia/Shanghai")
    parser.add_argument("--user-agent", default=None, help="自定义 User-Agent")
    parser.add_argument("--auto-disable", action="store_true",
                        help="抓取失败的源自动在订阅文件中注释禁用")
    parser.add_argument("--verbose", action="store_true", help="输出每源的详细日志")
    args = parser.parse_args()

    init_alias(args.alias)

    channel_names = load_channel_names(args.channels)
    print(f"[EPG] 读取频道 {len(channel_names)} 个（来自 {args.channels}）")
    if not channel_names:
        print("[EPG] 未读取到任何频道，请检查 --channels 文件")
        sys.exit(1)
    print("[EPG] 匹配流程：先按频道原名/规范化名称直接匹配，未匹配到的再尝试别名表解析，"
          "结果保留 demo.txt 中的原始频道名")

    extra_entries = [{"url": u, "_priority": 2} for u in args.extra_epg_url]

    epg_data = get_epg(
        channel_names,
        epg_path=args.epg_sources,
        extra_entries=extra_entries,
        include_unmatched=args.include_unmatched,
        max_programmes=args.max_programmes,
        days_back=args.days_back,
        days_ahead=args.days_ahead,
        fetch_concurrency=args.fetch_concurrency,
        request_timeout=args.request_timeout,
        proxy=args.proxy,
        auto_disable=args.auto_disable,
        user_agent=args.user_agent,
        verbose=args.verbose,
    )

    if not epg_data:
        print("[EPG] 未匹配到任何频道节目，未生成输出文件")
        sys.exit(2)

    write_to_xml(epg_data, args.output_xml, timezone=args.timezone)
    print(f"[EPG] 已输出 XML 节目单：{args.output_xml}")
    if not args.no_gz:
        compress_to_gz(args.output_xml, args.output_gz)
        print(f"[EPG] 已输出 gzip 压缩版：{args.output_gz}")

    matched = sorted(epg_data.keys())
    print(f"[EPG] 共 {len(matched)} 个频道匹配到节目：{'、'.join(matched)}")


if __name__ == "__main__":
    main()