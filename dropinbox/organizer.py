"""扫描目录、规划去向、执行搬运。

所有写操作都收敛在这里，便于审计：
- 永远不覆盖已有文件（重名自动加 (1)）
- 永远不把文件搬到它自己所在的目录
- 默认不跨越 archive_root 之外的路径
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import history
from .config import expand
from .rules import (
    FileContext,
    Rule,
    category_of,
    build_context,
    evaluate,
    rules_sorted,
)

__all__ = ["Decision", "collect_files", "plan", "apply_plan", "render_target", "is_inside"]

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')


@dataclass
class Decision:
    path: Path
    size: int
    rule_id: str
    rule_name: str
    target: Optional[Path]
    reason: str
    action: str = "move"  # move | skip
    src_display: str = ""
    source_domain: str = ""
    target_rel: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "src_display": self.src_display or str(self.path),
            "name": self.path.name,
            "size": self.size,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "target": str(self.target) if self.target else "",
            "target_rel": self.target_rel or (str(self.target) if self.target else ""),
            "reason": self.reason,
            "action": self.action,
            "source_domain": self.source_domain,
        }


def watch_roots(config: Dict[str, Any]) -> List[Path]:
    roots: List[Path] = []
    for entry in config.get("watch_dirs", []) or []:
        p = expand(str(entry))
        if p not in roots:
            roots.append(p)
    return roots


def archive_root(config: Dict[str, Any]) -> Path:
    return expand(str(config.get("archive_root") or "~/Downloads/_Inbox"))


def is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _is_ignored(path: Path, config: Dict[str, Any]) -> bool:
    name = path.name
    if config.get("ignore_hidden", True) and name.startswith("."):
        return True
    for pattern in config.get("ignore_globs", []) or []:
        if fnmatch.fnmatch(name.lower(), str(pattern).lower()):
            return True
    return False


def collect_files(config: Dict[str, Any]) -> List[Path]:
    """列出监听目录里当前所有"看起来是给用户看的"文件。"""
    root = archive_root(config)
    found: List[Path] = []
    seen = set()
    for base in watch_roots(config):
        if not base.exists() or not base.is_dir():
            continue
        walker: Iterable[Path]
        if config.get("recursive"):
            walker = (p for p in base.rglob("*") if p.is_file())
        else:
            walker = (p for p in base.iterdir() if p.is_file())
        for path in walker:
            try:
                key = str(path.resolve()).lower()
            except OSError:
                key = str(path).lower()
            if key in seen:
                continue
            if _is_ignored(path, config):
                continue
            if is_inside(path, root):
                continue
            seen.add(key)
            found.append(path)
    found.sort(key=lambda p: p.name.lower())
    return found


def _sanitize_segment(segment: str) -> str:
    cleaned = _ILLEGAL.sub("_", segment).strip().strip(".")
    return cleaned or "未命名"


def render_target(template: str, ctx: FileContext, root: Path) -> Path:
    """把 "文档/{year}/{ext}" 渲染成绝对路径。

    支持占位符：{year} {month} {day} {ext} {stem} {name} {category} {source}
    未知占位符会被替换为空，不会抛异常 —— 规则写错不该让整个流程崩掉。
    """
    when = time.localtime(ctx.mtime)
    try:
        source_domain = ctx.source.domain if ctx.source else ""
    except Exception:
        source_domain = ""
    values = {
        "year": f"{when.tm_year:04d}",
        "month": f"{when.tm_mon:02d}",
        "day": f"{when.tm_mday:02d}",
        "ext": ctx.ext,
        "stem": ctx.path.stem,
        "name": ctx.path.name,
        "category": category_of(ctx.path) or "other",
        "source": source_domain or "unknown",
    }

    def repl(match: "re.Match[str]") -> str:
        return str(values.get(match.group(1), ""))

    rendered = re.sub(r"\{([a-zA-Z_]+)\}", repl, template or "")
    parts = [p for p in re.split(r"[\\/]+", rendered) if p and p not in (".", "..")]
    if not parts:
        parts = ["未分类"]
    parts = [_sanitize_segment(p) for p in parts]
    return root.joinpath(*parts)


def plan(
    config: Dict[str, Any],
    raw_rules: List[Dict[str, Any]],
    paths: Optional[Sequence[Path]] = None,
    now: Optional[float] = None,
) -> List[Decision]:
    """不产生任何副作用的预演：每个文件该去哪、因为哪条规则。"""
    current = now if now is not None else time.time()
    rules: List[Rule] = rules_sorted(raw_rules)
    root = archive_root(config)
    targets = paths if paths is not None else collect_files(config)

    decisions: List[Decision] = []
    for path in targets:
        ctx = build_context(path, current)
        if ctx is None:
            continue
        min_age = float(config.get("min_age_seconds", 0) or 0)
        if ctx.age_seconds < min_age:
            decisions.append(
                Decision(
                    path=path,
                    size=ctx.size,
                    rule_id="",
                    rule_name="",
                    target=None,
                    reason=f"刚出现 {ctx.age_seconds:.0f}s，等待写盘完成",
                    action="skip",
                )
            )
            continue

        matched: Optional[Rule] = None
        reason = "没有规则命中"
        for rule in rules:
            ok, why = evaluate(rule, ctx)
            if ok:
                matched = rule
                reason = why
                break

        if matched is None:
            decisions.append(
                Decision(
                    path=path,
                    size=ctx.size,
                    rule_id="",
                    rule_name="",
                    target=None,
                    reason=reason,
                    action="skip",
                )
            )
            continue

        target_dir = render_target(matched.target_template(), ctx, root)
        try:
            target_rel = target_dir.relative_to(root).as_posix()
        except ValueError:
            target_rel = str(target_dir)
        if target_dir.resolve() == path.parent.resolve():
            decisions.append(
                Decision(
                    path=path,
                    size=ctx.size,
                    rule_id=matched.id,
                    rule_name=matched.name,
                    target=None,
                    reason="已经在该目录里，无需移动",
                    action="skip",
                )
            )
            continue

        decisions.append(
            Decision(
                path=path,
                size=ctx.size,
                rule_id=matched.id,
                rule_name=matched.name,
                target=target_dir,
                reason=reason,
                action="move",
                source_domain=(ctx.source.domain if ctx.source else ""),
                target_rel=target_rel,
            )
        )
    return decisions


def _free_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"目标重名过多：{path}")


def apply_plan(
    decisions: Sequence[Decision],
    kind: str = "manual",
    note: str = "",
    record: bool = True,
    keep_history: int = 200,
) -> Dict[str, Any]:
    """执行预演结果，返回批次信息（可用于立刻撤销）。"""
    moves = [d for d in decisions if d.action == "move" and d.target is not None]
    result: Dict[str, Any] = {
        "batch_id": None,
        "moved": 0,
        "failed": 0,
        "errors": [],
        "items": [],
    }
    if not moves:
        return result

    batch_id = history.create_batch(kind=kind, note=note) if record else None
    result["batch_id"] = batch_id

    for decision in moves:
        src = decision.path
        try:
            # decision.target 是目标"目录"，最终路径必须保留原文件名
            target_dir = decision.target
            target_dir.mkdir(parents=True, exist_ok=True)
            final = _free_path(target_dir / src.name)
            shutil.move(str(src), str(final))
        except OSError as exc:
            result["failed"] += 1
            result["errors"].append(f"{src.name}: {exc}")
            continue

        result["moved"] += 1
        result["items"].append({"src": str(src), "dst": str(final)})
        if record and batch_id:
            history.record_operation(
                batch_id, src=src, dst=final, size=decision.size, rule_id=decision.rule_id
            )

    if record and keep_history > 0:
        history.prune(keep=keep_history)
    return result
