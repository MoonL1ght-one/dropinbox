"""规则引擎：把一条 JSON 规则变成"这个文件该不该走、为什么"。

规则可选条件（全部可选，留空即不约束）：
  ext                扩展名列表，如 ["pdf", "docx"]
  categories         内置类别，如 ["images", "documents"]
  name_regex         文件名的正则
  name_contains      文件名包含的任意一个子串
  content_keywords   文件内容（前 16KB 文本）包含的任意一个关键词
  source_domains     下载来源域名后缀，如 ["github.com"]
  size_min/size_max  大小区间，如 "1MB"
  older_than/newer_than  文件年龄，如 "2h" / "30m" / "7d"

match_mode:
  "all"  所有条件都满足（默认）
  "any"  任一条件满足

规则里一个条件都没写 => 恒真，用来做兜底规则。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .meta import SourceInfo, domain_of, read_source

__all__ = [
    "CATEGORIES",
    "FileContext",
    "Rule",
    "category_of",
    "parse_size",
    "parse_duration",
    "build_context",
    "rules_sorted",
    "evaluate",
]

# ---------------------------------------------------------------- 类别映射

CATEGORIES: Dict[str, List[str]] = {
    "images": [
        "jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "tif", "tiff",
        "heic", "heif", "svg", "ico", "psd", "raw", "cr2", "nef", "arw",
    ],
    "videos": [
        "mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v", "mpg",
        "mpeg", "ts", "3gp", "rmvb",
    ],
    "audio": [
        "mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus", "aiff", "mid",
    ],
    "archives": [
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz", "zst", "iso", "cab",
    ],
    "documents": [
        "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "pages", "wps", "tex",
    ],
    "spreadsheets": ["xls", "xlsx", "xlsm", "csv", "tsv", "ods", "numbers"],
    "presentations": ["ppt", "pptx", "pps", "ppsx", "key", "odp"],
    "ebooks": ["epub", "mobi", "azw", "azw3", "fb2", "djvu", "cbz", "cbr"],
    "fonts": ["ttf", "otf", "woff", "woff2", "eot", "ttc"],
    "code": [
        "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "xml", "html",
        "css", "sh", "bat", "ps1", "sql", "ipynb", "go", "rs", "java", "c",
        "h", "cpp", "hpp", "rb", "php", "lua", "toml", "ini", "log",
    ],
    "models": ["obj", "fbx", "stl", "gltf", "glb", "blend", "step", "stp", "3ds"],
}

_EXT_TO_CATEGORIES: Dict[str, List[str]] = {}
for _cat, _exts in CATEGORIES.items():
    for _e in _exts:
        _EXT_TO_CATEGORIES.setdefault(_e, []).append(_cat)

# 视为纯文本、可以读来做关键词匹配的扩展名
TEXTUAL_EXTS = {
    "txt", "md", "csv", "tsv", "json", "xml", "yml", "yaml", "ini", "log",
    "html", "htm", "rtf", "tex", "py", "js", "ts", "css", "sh", "sql", "toml",
}
MAX_CONTENT_BYTES = 16 * 1024


def category_of(path: Path) -> Optional[str]:
    """返回文件所属类别（第一个命中的），未知类型返回 None。"""
    ext = path.suffix.lower().lstrip(".")
    cats = _EXT_TO_CATEGORIES.get(ext)
    return cats[0] if cats else None


# ---------------------------------------------------------------- 单位解析

_SIZE_UNITS = {
    "": 1, "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
}
_DURATION_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_size(value: Any) -> int:
    """'10KB' / '1.5MB' / 2048 -> 字节数。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace(" ", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([a-z]*)", text)
    if not match:
        raise ValueError(f"无法解析大小: {value!r}")
    number, unit = match.group(1), match.group(2)
    if unit not in _SIZE_UNITS:
        raise ValueError(f"未知大小单位: {unit!r}")
    return int(float(number) * _SIZE_UNITS[unit])


def parse_duration(value: Any) -> float:
    """'2h' / '90s' / '7d' -> 秒数。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(" ", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([a-z]*)", text)
    if not match:
        raise ValueError(f"无法解析时长: {value!r}")
    number, unit = match.group(1), match.group(2)
    if unit not in _DURATION_UNITS:
        raise ValueError(f"未知时长单位: {unit!r}")
    return float(number) * _DURATION_UNITS[unit]


# ---------------------------------------------------------------- 上下文

@dataclass
class FileContext:
    """一个待判定文件的全部可用信息（内容与来源按需惰性读取）。"""

    path: Path
    size: int
    mtime: float
    now: float
    _text: Optional[str] = field(default=None, repr=False)
    _source: Optional[SourceInfo] = field(default=None, repr=False)
    _text_loaded: bool = field(default=False, repr=False)
    _source_loaded: bool = field(default=False, repr=False)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower().lstrip(".")

    @property
    def age_seconds(self) -> float:
        return max(0.0, self.now - self.mtime)

    @property
    def text(self) -> str:
        if not self._text_loaded:
            self._text_loaded = True
            self._text = _read_text_head(self.path, self.ext)
        return self._text or ""

    @property
    def source(self) -> Optional[SourceInfo]:
        if not self._source_loaded:
            self._source_loaded = True
            self._source = read_source(self.path)
        return self._source


def _read_text_head(path: Path, ext: str) -> str:
    if ext not in TEXTUAL_EXTS:
        return ""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_CONTENT_BYTES)
    except OSError:
        return ""
    for encoding in ("utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def build_context(path: Path, now: float) -> Optional[FileContext]:
    try:
        st = path.stat()
    except OSError:
        return None
    return FileContext(path=path, size=st.st_size, mtime=st.st_mtime, now=now)


# ---------------------------------------------------------------- 规则

@dataclass
class Rule:
    id: str
    name: str
    target: str
    match: Dict[str, Any] = field(default_factory=dict)
    match_mode: str = "all"
    priority: int = 100
    enabled: bool = True
    action: str = "move"

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int = 0) -> "Rule":
        rid = str(data.get("id") or f"rule-{index + 1}")
        match = data.get("match") or {}
        if not isinstance(match, dict):
            match = {}
        mode = str(data.get("match_mode") or "all").lower()
        if mode not in ("all", "any"):
            mode = "all"
        try:
            priority = int(data.get("priority", 100))
        except (TypeError, ValueError):
            priority = 100
        return cls(
            id=rid,
            name=str(data.get("name") or rid),
            target=str(data.get("target") or "未分类"),
            match=match,
            match_mode=mode,
            priority=priority,
            enabled=bool(data.get("enabled", True)),
            action=str(data.get("action") or "move"),
        )

    def target_template(self) -> str:
        return self.target.strip() or "未分类"


def rules_sorted(raw_rules: List[Dict[str, Any]]) -> List[Rule]:
    """按 priority 升序；同优先级保持 JSON 里的先后顺序（稳定排序）。"""
    rules = [Rule.from_dict(r, i) for i, r in enumerate(raw_rules)]
    rules = [r for r in rules if r.enabled]
    rules.sort(key=lambda r: r.priority)
    return rules


# ---------------------------------------------------------------- 条件求值
# 每个检查函数返回 (是否命中, 人类可读说明) 或 None 表示该条件未设置。

def _check_ext(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    wanted = [str(x).lower().lstrip(".") for x in _as_list(value)]
    if not wanted:
        return None
    return (ctx.ext in wanted, f"扩展名 {ctx.ext or '(无)'}")


def _check_categories(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    wanted = [str(x).lower() for x in _as_list(value)]
    if not wanted:
        return None
    cats = _EXT_TO_CATEGORIES.get(ctx.ext, [])
    hit = any(c in cats for c in wanted)
    return (hit, f"类别 {'/'.join(wanted)}")


def _check_name_regex(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    pattern = str(value or "").strip()
    if not pattern:
        return None
    try:
        hit = re.search(pattern, ctx.path.name) is not None
    except re.error:
        return (False, f"正则无效: {pattern}")
    return (hit, f"文件名匹配 /{pattern}/")


def _check_name_contains(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    needles = [str(x) for x in _as_list(value) if str(x)]
    if not needles:
        return None
    name = ctx.path.name.lower()
    found = [n for n in needles if n.lower() in name]
    return (bool(found), "文件名含 " + "、".join(found) if found else "文件名不含关键词")


def _check_content(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    needles = [str(x) for x in _as_list(value) if str(x)]
    if not needles:
        return None
    text = ctx.text.lower()
    if not text:
        return (False, "内容不可读")
    found = [n for n in needles if n.lower() in text]
    if found:
        return (True, "内容含 " + "、".join(found))
    return (False, "内容不含关键词")


def _check_source(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    domains = [domain_of(str(d)) or str(d).lower().lstrip(".") for d in _as_list(value)]
    domains = [d for d in domains if d]
    if not domains:
        return None
    info = ctx.source
    if info is None:
        return (False, "无来源信息")
    host = info.domain
    for d in domains:
        if host == d or host.endswith("." + d):
            return (True, f"来源 {host}")
    return (False, f"来源 {host or '未知'}")


def _check_size_min(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    if value in (None, ""):
        return None
    limit = parse_size(value)
    return (ctx.size >= limit, f"大小 ≥ {_human_size(limit)}")


def _check_size_max(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    if value in (None, ""):
        return None
    limit = parse_size(value)
    return (ctx.size <= limit, f"大小 ≤ {_human_size(limit)}")


def _check_older(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    if value in (None, ""):
        return None
    limit = parse_duration(value)
    return (ctx.age_seconds >= limit, f"已存在 ≥ {_human_duration(limit)}")


def _check_newer(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    if value in (None, ""):
        return None
    limit = parse_duration(value)
    return (ctx.age_seconds <= limit, f"已存在 ≤ {_human_duration(limit)}")


def _check_glob(value: Any, ctx: FileContext) -> Optional[Tuple[bool, str]]:
    patterns = [str(x) for x in _as_list(value) if str(x)]
    if not patterns:
        return None
    hit = any(fnmatch.fnmatch(ctx.path.name.lower(), p.lower()) for p in patterns)
    return (hit, "文件名通配 " + "、".join(patterns))


CHECKS = [
    ("ext", _check_ext),
    ("categories", _check_categories),
    ("name_regex", _check_name_regex),
    ("name_contains", _check_name_contains),
    ("name_glob", _check_glob),
    ("content_keywords", _check_content),
    ("source_domains", _check_source),
    ("size_min", _check_size_min),
    ("size_max", _check_size_max),
    ("older_than", _check_older),
    ("newer_than", _check_newer),
]


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [value]
    return [value]


def evaluate(rule: Rule, ctx: FileContext) -> Tuple[bool, str]:
    """返回 (是否命中, 命中原因)。"""
    results: List[Tuple[bool, str]] = []
    for key, fn in CHECKS:
        if key not in rule.match:
            continue
        try:
            outcome = fn(rule.match.get(key), ctx)
        except (ValueError, TypeError) as exc:
            outcome = (False, f"{key} 配置有误: {exc}")
        if outcome is not None:
            results.append(outcome)

    if not results:
        return True, "兜底规则（无条件）"

    if rule.match_mode == "any":
        hits = [reason for ok, reason in results if ok]
        if hits:
            return True, "、".join(hits)
        missed = "、".join(reason for _, reason in results)
        return False, f"任一条件均未满足（{missed}）"

    misses = [reason for ok, reason in results if not ok]
    if misses:
        return False, "、".join(misses) + " 不满足"
    return True, "、".join(reason for _, reason in results)


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


def _human_duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.0f}天"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}小时"
    if seconds >= 60:
        return f"{seconds / 60:.0f}分钟"
    return f"{seconds:.0f}秒"
