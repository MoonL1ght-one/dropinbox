"""读取"这个文件是从哪下载的"。

这是 DropInbox 相对同类工具比较有意思的一点：不需要联网、不需要浏览器插件，
直接从操作系统留下的下载元数据里取来源。

- Windows: NTFS 备用数据流 ``Zone.Identifier``（浏览器下载时由系统写入）
- macOS:   扩展属性 ``com.apple.metadata:kMDItemWhereFroms``
- Linux:   扩展属性 ``user.xdg.origin.url``（部分浏览器/下载器）

全部读取失败时安静返回 None —— 来源条件只是可选加分项，不是必需条件。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

__all__ = ["SourceInfo", "read_source", "domain_of"]


@dataclass(frozen=True)
class SourceInfo:
    url: str
    referrer: str = ""

    @property
    def domain(self) -> str:
        return domain_of(self.referrer or self.url)


def domain_of(url: str) -> str:
    """取出可比较的主机名：去掉协议、端口、www. 前缀。"""
    if not url:
        return ""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        host = urlparse(candidate).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _parse_zone_identifier(text: str) -> Optional[SourceInfo]:
    fields = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    host_url = fields.get("HostUrl", "")
    referrer = fields.get("ReferrerUrl", "")
    # ZoneId=3 表示来自 Internet；本地文件没有实际下载来源，直接忽略
    if not host_url and not referrer:
        return None
    if host_url in ("about:internet", ""):
        host_url = ""
    return SourceInfo(url=host_url, referrer=referrer)


def _windows_source(path: Path) -> Optional[SourceInfo]:
    try:
        stream = f"{path}:Zone.Identifier"
        with open(stream, "r", encoding="utf-8", errors="replace") as f:
            return _parse_zone_identifier(f.read())
    except OSError:
        return None


def _xattr_bytes(path: Path, name: str) -> Optional[bytes]:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        return getxattr(str(path), name)
    except (OSError, AttributeError):
        return None


def _macos_source(path: Path) -> Optional[SourceInfo]:
    raw = _xattr_bytes(path, "com.apple.metadata:kMDItemWhereFroms")
    if not raw:
        return None
    try:
        import plistlib

        items = plistlib.loads(raw)
    except Exception:
        return None
    if not isinstance(items, list) or not items:
        return None
    urls = [str(x) for x in items if isinstance(x, str) and x.startswith("http")]
    if not urls:
        return None
    primary = urls[0]
    referrer = urls[1] if len(urls) > 1 else ""
    return SourceInfo(url=primary, referrer=referrer)


def _linux_source(path: Path) -> Optional[SourceInfo]:
    raw = _xattr_bytes(path, "user.xdg.origin.url")
    if not raw:
        raw = _xattr_bytes(path, "user.xdg.referrer.url")
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")
        return SourceInfo(url="", referrer=text)
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    return SourceInfo(url=text)


def read_source(path: Path) -> Optional[SourceInfo]:
    """尽力而为地读取下载来源，失败返回 None。"""
    p = Path(path)
    try:
        if sys.platform.startswith("win"):
            info = _windows_source(p)
            if info:
                return info
        elif sys.platform == "darwin":
            info = _macos_source(p)
            if info:
                return info
        else:
            info = _linux_source(p)
            if info:
                return info
        # 兜底：某些环境（含 WSL 挂载的 NTFS）可能走另一条路
        return _linux_source(p)
    except Exception:
        return None
