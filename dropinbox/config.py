"""配置与默认规则。

设计原则：所有状态都是普通文件（config.json / rules.json / history.db），
放在 ~/.dropinbox 下，随时可以用文本编辑器打开、diff、备份、迁移。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

APP_NAME = "dropinbox"


def app_dir() -> Path:
    """运行时数据目录，可用 DROPINBOX_HOME 覆盖（测试与便携模式用）。"""
    override = os.environ.get("DROPINBOX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dropinbox"


def config_path() -> Path:
    return app_dir() / "config.json"


def rules_path() -> Path:
    return app_dir() / "rules.json"


def db_path() -> Path:
    return app_dir() / "history.db"


DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    # 监听哪些目录（支持 ~ 与环境变量）
    "watch_dirs": ["~/Downloads"],
    # 归档到哪，规则里的 target 都相对这个目录
    "archive_root": "~/Downloads/_Inbox",
    # 是否递归监听子目录
    "recursive": False,
    # 轮询间隔（秒）。不用 inotify 是为了三平台行为一致、零依赖
    "poll_interval": 2.0,
    # 文件"落盘稳定"判定：大小与修改时间连续这么久不变才算下载完成
    "settle_seconds": 1.5,
    # 才算有效文件的最小年龄，避免浏览器还没写完就被搬走
    "min_age_seconds": 2,
    # 永远不碰的文件
    "ignore_globs": [
        "*.crdownload",
        "*.part",
        "*.partial",
        "*.download",
        "*.tmp",
        "*.temp",
        "*.!ut",
        "~$*",
        ".DS_Store",
        "desktop.ini",
        "Thumbs.db",
    ],
    "ignore_hidden": True,
    # 历史保留批次数（撤销依赖它）
    "history_limit": 200,
}

# 默认规则：开箱即用，但全部可以在 GUI 里改，也可以直接编辑 rules.json。
# priority 越小越先匹配；命中第一条即停止。
DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "id": "invoice",
        "name": "发票 / 账单",
        "enabled": True,
        "priority": 10,
        "match_mode": "any",
        "match": {
            "name_regex": r"(?i)(invoice|receipt|发票|账单|对账单|报销)",
            "content_keywords": ["发票", "增值税", "电子发票", "invoice"],
        },
        "target": "财务/发票",
    },
    {
        "id": "screenshot",
        "name": "截图 / 录屏",
        "enabled": True,
        "priority": 20,
        "match_mode": "any",
        "match": {
            "name_regex": r"(?i)(screenshot|screen shot|snipaste|截图|截屏|屏幕快照)",
        },
        "target": "图片/截图",
    },
    {
        "id": "installer",
        "name": "安装包",
        "enabled": True,
        "priority": 90,
        "match_mode": "all",
        "match": {
            "ext": ["exe", "msi", "msix", "dmg", "pkg", "deb", "rpm", "apk", "appimage"],
        },
        "target": "软件/安装包",
    },
    {
        "id": "images",
        "name": "图片",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["images"]},
        "target": "图片",
    },
    {
        "id": "videos",
        "name": "视频",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["videos"]},
        "target": "媒体/视频",
    },
    {
        "id": "audio",
        "name": "音频",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["audio"]},
        "target": "媒体/音频",
    },
    {
        "id": "archives",
        "name": "压缩包",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["archives"]},
        "target": "归档/压缩包",
    },
    {
        "id": "fonts",
        "name": "字体",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["fonts"]},
        "target": "设计/字体",
    },
    {
        "id": "models3d",
        "name": "3D / 模型",
        "enabled": True,
        "priority": 100,
        "match_mode": "all",
        "match": {"categories": ["models"]},
        "target": "设计/3D",
    },
    {
        "id": "ebooks",
        "name": "电子书",
        "enabled": True,
        "priority": 105,
        "match_mode": "all",
        "match": {"categories": ["ebooks"]},
        "target": "文档/电子书",
    },
    {
        "id": "spreadsheets",
        "name": "表格",
        "enabled": True,
        "priority": 110,
        "match_mode": "all",
        "match": {"categories": ["spreadsheets"]},
        "target": "文档/表格",
    },
    {
        "id": "presentations",
        "name": "演示文稿",
        "enabled": True,
        "priority": 110,
        "match_mode": "all",
        "match": {"categories": ["presentations"]},
        "target": "文档/演示",
    },
    {
        "id": "documents",
        "name": "文档",
        "enabled": True,
        "priority": 120,
        "match_mode": "all",
        "match": {"categories": ["documents"]},
        "target": "文档",
    },
    {
        "id": "code",
        "name": "代码 / 数据",
        "enabled": True,
        "priority": 130,
        "match_mode": "all",
        "match": {"categories": ["code"]},
        "target": "开发",
    },
    {
        "id": "from-github",
        "name": "来自 GitHub",
        "enabled": False,
        "priority": 60,
        "match_mode": "all",
        "match": {"source_domains": ["github.com", "githubusercontent.com"]},
        "target": "开发/来自GitHub",
    },
    {
        "id": "fallback",
        "name": "未分类（兜底）",
        "enabled": True,
        "priority": 9999,
        "match_mode": "all",
        "match": {},
        "target": "未分类",
    },
]


def expand(path_like: str) -> Path:
    """展开 ~、环境变量，并转成绝对路径。"""
    raw = str(path_like).strip()
    raw = os.path.expandvars(raw)
    return Path(raw).expanduser()


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or config_path()
    data: Dict[str, Any] = {}
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    return merged


def save_config(cfg: Dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return p


def load_rules(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or rules_path()
    if not p.exists():
        return [dict(r) for r in DEFAULT_RULES]
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rules", [])
    return [r for r in data if isinstance(r, dict)]


def save_rules(rules: List[Dict[str, Any]], path: Optional[Path] = None) -> Path:
    p = path or rules_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(list(rules), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return p


def is_initialized() -> bool:
    return config_path().exists()


def initialize(force: bool = False) -> Dict[str, Path]:
    """写入默认配置与规则，已存在则跳过（除非 force）。"""
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    if force and config_path().exists():
        shutil.copy2(config_path(), config_path().with_suffix(".json.bak"))
    if force or not config_path().exists():
        written["config"] = save_config(load_config() if not force else dict(DEFAULT_CONFIG))

    if force and rules_path().exists():
        shutil.copy2(rules_path(), rules_path().with_suffix(".json.bak"))
    if force or not rules_path().exists():
        written["rules"] = save_rules([dict(r) for r in DEFAULT_RULES])

    return written
