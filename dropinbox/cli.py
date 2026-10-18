"""DropInbox 命令行。

  dropinbox init            写入默认配置与规则
  dropinbox scan            预览：文件会去哪
  dropinbox apply           真正执行归档
  dropinbox watch           盯着下载目录，下载完成即自动归档
  dropinbox undo            撤销上一次归档
  dropinbox rules list      查看规则
  dropinbox gui             打开本地图形界面
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, config as cfgmod, history
from .organizer import apply_plan, archive_root, plan, watch_roots
from .rules import Rule, rules_sorted
from .watcher import Watcher

PROG = "dropinbox"


# ------------------------------------------------------------------ 输出美化

class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)


def _enable_windows_ansi() -> bool:
    """Windows 10+ 的控制台需要显式打开 VT 处理，否则颜色码会显示成乱字符。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def _style() -> Style:
    if os.environ.get("NO_COLOR"):
        return Style(False)
    if os.environ.get("FORCE_COLOR"):
        return Style(True)
    if not sys.stdout.isatty():
        return Style(False)
    if os.name == "nt":
        # 老式 conhost 不解析 ANSI 转义，会把颜色码显示成乱字符。
        # 只有在 Windows Terminal / ConEmu 这类现代终端里才上色。
        modern = (
            os.environ.get("WT_SESSION")
            or os.environ.get("TERM_PROGRAM")
            or os.environ.get("ConEmuANSI")
        )
        if not modern:
            return Style(False)
        return Style(_enable_windows_ansi())
    return Style(True)


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if _is_wide(ch) else 1
    return width


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAFF
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        w = 2 if _is_wide(ch) else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _rel(path: Path) -> str:
    try:
        home = Path.home()
        return "~/" + str(path.resolve().relative_to(home.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(path)


def _ensure_ready() -> None:
    if not cfgmod.is_initialized():
        cfgmod.initialize()
        print(f"首次运行，已创建配置文件: {cfgmod.app_dir()}")
        print()


# ------------------------------------------------------------------ 命令实现

def cmd_init(args: argparse.Namespace) -> int:
    written = cfgmod.initialize(force=args.force)
    history.init_db()
    s = _style()
    print(s.bold(f"DropInbox {__version__}"))
    print(f"配置目录: {cfgmod.app_dir()}")
    if written:
        for label, path in written.items():
            print(f"  已写入 {label}: {path}")
    else:
        print("  配置已存在，未改动（加 --force 可重置，会先备份 .bak）")
    print()
    print("下一步:")
    print(f"  {s.cyan(PROG + ' scan')}    看看下载目录里的文件会被归到哪")
    print(f"  {s.cyan(PROG + ' gui')}     打开图形界面")
    return 0


def _print_plan(decisions: List[Any], root: Path) -> None:
    s = _style()
    moves = [d for d in decisions if d.action == "move"]
    skips = [d for d in decisions if d.action != "move"]

    if not moves and not skips:
        print(s.dim("没有发现可归档的文件。"))
        return

    if moves:
        name_w = min(46, max([_display_width(d.path.name) for d in moves] + [10]))
        rule_w = min(18, max([_display_width(d.rule_name or "-") for d in moves] + [8]))
        total = sum(d.size for d in moves)
        print(f"{s.bold(str(len(moves)))} 个文件待归档，共 {human_size(total)} → {_rel(root)}")
        print()
        for d in moves:
            name = _pad(_truncate(d.path.name, name_w), name_w)
            rule = _pad(_truncate(d.rule_name or "-", rule_w), rule_w)
            target = _rel(d.target) if d.target else "-"
            print(f"  {name}  {s.cyan(rule)}  {human_size(d.size):>9}  →  {target}")
        print()

    if skips and os.environ.get("DROPINBOX_VERBOSE"):
        print(s.dim(f"跳过 {len(skips)} 个："))
        for d in skips:
            print(s.dim(f"  {_truncate(d.path.name, 50)}  ({d.reason})"))
        print()


def cmd_scan(args: argparse.Namespace) -> int:
    _ensure_ready()
    cfg = cfgmod.load_config()
    rules = cfgmod.load_rules()
    decisions = plan(cfg, rules)
    _print_plan(decisions, archive_root(cfg))
    moves = [d for d in decisions if d.action == "move"]
    if moves:
        s = _style()
        print(s.dim(f"确认无误后执行：{PROG} apply"))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    _ensure_ready()
    cfg = cfgmod.load_config()
    rules = cfgmod.load_rules()
    decisions = [d for d in plan(cfg, rules) if d.action == "move"]
    s = _style()
    if not decisions:
        print("没有需要归档的文件。")
        return 0

    if not args.yes:
        _print_plan(decisions, archive_root(cfg))
        try:
            answer = input(f"归档这 {len(decisions)} 个文件？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print("已取消。")
            return 1

    result = apply_plan(
        decisions,
        kind="cli",
        note="命令行归档",
        keep_history=int(cfg.get("history_limit", 200) or 200),
    )
    print(s.green(f"已归档 {result['moved']} 个文件"))
    if result["failed"]:
        print(s.yellow(f"失败 {result['failed']} 个"))
        for err in result["errors"][:10]:
            print(s.red(f"  {err}"))
    if result.get("batch_id"):
        print(s.dim(f"不满意？撤销：{PROG} undo"))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    _ensure_ready()
    cfg = cfgmod.load_config()
    rules = cfgmod.load_rules()
    s = _style()

    dirs = watch_roots(cfg)
    missing = [d for d in dirs if not d.exists()]
    for d in missing:
        print(s.yellow(f"监听目录不存在，已跳过: {d}"))
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        print(s.red("没有可监听的目录，请先修改配置："))
        print(f"  {cfgmod.config_path()}")
        return 1

    dry = bool(args.dry_run)
    print(s.bold(f"DropInbox {__version__} · 监听中") + (" (预演模式，不移动文件)" if dry else ""))
    print(f"  监听: {', '.join(_rel(d) for d in dirs)}")
    print(f"  归档: {_rel(archive_root(cfg))}")
    print(s.dim("  Ctrl+C 停止"))
    print()

    def on_batch(result: Dict[str, Any]) -> None:
        stamp = time.strftime("%H:%M:%S")
        for item in result.get("items", []):
            print(f"  {s.dim(stamp)}  {Path(item['src']).name}  →  {_rel(Path(item['dst']).parent)}")
        if result.get("failed"):
            print(s.yellow(f"  {stamp}  {result['failed']} 个文件移动失败"))

    watcher = Watcher(cfg, rules, dry_run=dry, on_batch=on_batch)
    watcher.start()
    try:
        while watcher.running:
            time.sleep(0.4)
    except KeyboardInterrupt:
        watcher.stop()
        print()
        print(s.dim(f"已停止。本次共归档 {watcher.total_moved} 个文件。"))
        if watcher.total_moved:
            print(s.dim(f"撤销：{PROG} undo"))
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    _ensure_ready()
    s = _style()
    history.init_db()

    if args.list:
        batches = history.list_batches(limit=20)
        if not batches:
            print("还没有任何归档记录。")
            return 0
        print(s.bold("最近的归档批次"))
        for b in batches:
            mark = {"applied": s.green("可撤销"), "undone": s.dim("已撤销"), "partial": s.yellow("部分撤销")}.get(
                b.status, b.status
            )
            print(
                f"  #{b.id:<4} {b.created_at}  {b.kind:<6} {b.count:>3} 个  "
                f"{human_size(b.bytes):>9}  {mark}"
            )
        print()
        print(s.dim(f"撤销某个批次：{PROG} undo --batch 12"))
        return 0

    if args.batch:
        batch_id = int(args.batch)
    else:
        batch_id = history.last_undoable_batch()
        if not batch_id:
            print("没有可撤销的批次。")
            return 0

    ops = [op for op in history.batch_operations(batch_id) if not op.undone]
    if not ops:
        print(f"批次 #{batch_id} 里没有待撤销的操作。")
        return 0

    if not args.yes:
        print(s.bold(f"撤销批次 #{batch_id}，把 {len(ops)} 个文件搬回原处："))
        for op in ops[:12]:
            print(f"  {_truncate(op.dst.name, 44)}  →  {_rel(op.src.parent)}")
        if len(ops) > 12:
            print(s.dim(f"  ...还有 {len(ops) - 12} 个"))
        try:
            answer = input("确认撤销？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print("已取消。")
            return 1

    result = history.undo_batch(batch_id)
    print(s.green(f"已还原 {result['restored']} 个文件"))
    if result["missing"]:
        print(s.yellow(f"{len(result['missing'])} 个文件已不在归档目录（可能被删除或移动过），跳过"))
    if result["errors"]:
        for err in result["errors"][:5]:
            print(s.red(f"  {err}"))
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    _ensure_ready()
    s = _style()

    if args.action in (None, "list"):
        rules = rules_sorted(cfgmod.load_rules())
        raw = {r.get("id"): r for r in cfgmod.load_rules()}
        if not rules:
            print("没有启用的规则。")
            return 0
        print(s.bold(f"{len(rules)} 条启用中的规则（按优先级）"))
        print()
        for r in rules:
            label = _pad(_truncate(r.name, 18), 18)
            cond = _truncate(json.dumps(r.match, ensure_ascii=False), 46)
            print(f"  {s.cyan(label)} {r.priority:>5}  {cond}  →  {r.target}")
        disabled = [r.get("name") for r in cfgmod.load_rules() if not r.get("enabled", True)]
        if disabled:
            print()
            print(s.dim("已禁用: " + "、".join(str(x) for x in disabled)))
        print()
        print(s.dim(f"编辑规则文件：{cfgmod.rules_path()}"))
        print(s.dim(f"用图形界面改：{PROG} gui"))
        return 0

    if args.action == "test":
        if not args.path:
            print(s.red("用法: dropinbox rules test <文件路径>"))
            return 1
        target = Path(args.path).expanduser()
        if not target.exists():
            print(s.red(f"文件不存在: {target}"))
            return 1
        from .rules import build_context

        ctx = build_context(target, time.time())
        assert ctx is not None
        print(f"文件: {target}")
        print(f"大小: {human_size(ctx.size)}   类别: {ctx.ext or '未知'}")
        src = ctx.source
        print(f"来源: {(src.referrer or src.url) if src else '(无下载来源信息)'}")
        print()
        raw = cfgmod.load_rules()
        rules = [Rule.from_dict(r, i) for i, r in enumerate(raw) if r.get("enabled", True)]
        rules.sort(key=lambda r: r.priority)
        hit = None
        for rule in rules:
            ok, why = rule_judge(rule, ctx)
            mark = s.green("命中") if ok else s.dim("未中")
            print(f"  {mark}  {_pad(_truncate(rule.name, 16), 16)}  {why}")
            if ok and hit is None:
                hit = rule
        print()
        if hit:
            print(s.green(f"→ 会被「{hit.name}」归档到 {hit.target}"))
        else:
            print("→ 没有规则命中，文件不会被动。")
        return 0

    if args.action == "path":
        print(cfgmod.rules_path())
        return 0

    if args.action == "edit":
        editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
        subprocess.call([editor, str(cfgmod.rules_path())])
        return 0

    if args.action == "export":
        dest = Path(args.path or "dropinbox-rules.json").expanduser()
        dest.write_text(
            json.dumps(cfgmod.load_rules(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已导出到 {dest}")
        return 0

    if args.action == "import":
        if not args.path:
            print(s.red("用法: dropinbox rules import <文件>"))
            return 1
        data = json.loads(Path(args.path).expanduser().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("rules", [])
        if not isinstance(data, list):
            print(s.red("导入失败：规则文件格式不对"))
            return 1
        cfgmod.save_rules(data)
        print(s.green(f"已导入 {len(data)} 条规则"))
        return 0

    print(s.red(f"未知操作: {args.action}"))
    return 1


def rule_judge(rule: Rule, ctx: Any):
    from .rules import evaluate

    return evaluate(rule, ctx)


def cmd_gui(args: argparse.Namespace) -> int:
    _ensure_ready()
    history.init_db()
    from .server import serve

    return serve(port=args.port, open_browser=not args.no_browser)


def cmd_status(args: argparse.Namespace) -> int:
    _ensure_ready()
    cfg = cfgmod.load_config()
    s = _style()
    stats = history.stats()
    print(s.bold(f"DropInbox {__version__}"))
    print(f"  配置目录   {cfgmod.app_dir()}")
    print(f"  监听目录   {', '.join(_rel(p) for p in watch_roots(cfg))}")
    print(f"  归档根目录 {_rel(archive_root(cfg))}")
    print()
    try:
        pending = len([d for d in plan(cfg, cfgmod.load_rules()) if d.action == "move"])
    except Exception:
        pending = 0
    print(f"  待归档     {pending} 个文件")
    print(f"  累计归档   {stats['files_sorted']} 个文件 / {stats['batches']} 个批次")
    if history.last_undoable_batch():
        print(f"  可撤销     {PROG} undo")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    _ensure_ready()
    cfg = cfgmod.load_config()
    s = _style()
    if args.action in (None, "show"):
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            print(s.red("用法: dropinbox config set <键> <值>"))
            return 1
        if args.key not in cfgmod.DEFAULT_CONFIG:
            print(s.red(f"未知配置项: {args.key}"))
            print(s.dim("可用: " + ", ".join(cfgmod.DEFAULT_CONFIG)))
            return 1
        current = cfg[args.key]
        value: Any = args.value
        if isinstance(current, bool):
            value = str(args.value).lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int):
            value = int(args.value)
        elif isinstance(current, float):
            value = float(args.value)
        elif isinstance(current, list) and args.append:
            current = list(current)
            current.append(args.value)
            value = current
        elif isinstance(current, list):
            value = [v.strip() for v in str(args.value).split(",") if v.strip()]
        cfg[args.key] = value
        cfgmod.save_config(cfg)
        print(f"{args.key} = {json.dumps(value, ensure_ascii=False)}")
        return 0
    print(s.red(f"未知操作: {args.action}"))
    return 1


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="本地优先的下载文件夹收件箱：按类型/来源/关键词自动归档，可预览、可撤销。",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化配置与默认规则")
    p_init.add_argument("--force", action="store_true", help="覆盖现有配置（先备份为 .bak）")
    p_init.set_defaults(func=cmd_init)

    p_scan = sub.add_parser("scan", help="预览：文件会被归到哪里（不移动）")
    p_scan.set_defaults(func=cmd_scan)

    p_apply = sub.add_parser("apply", help="执行归档")
    p_apply.add_argument("-y", "--yes", action="store_true", help="不再询问确认")
    p_apply.set_defaults(func=cmd_apply)

    p_watch = sub.add_parser("watch", help="持续监听并自动归档")
    p_watch.add_argument("--dry-run", action="store_true", help="只打印计划，不移动文件")
    p_watch.set_defaults(func=cmd_watch)

    p_undo = sub.add_parser("undo", help="撤销归档")
    p_undo.add_argument("--batch", type=int, help="指定批次号（默认最近一次）")
    p_undo.add_argument("--list", action="store_true", help="列出最近批次")
    p_undo.add_argument("-y", "--yes", action="store_true", help="不再询问确认")
    p_undo.set_defaults(func=cmd_undo)

    p_rules = sub.add_parser("rules", help="查看 / 测试 / 编辑规则")
    p_rules.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "test", "path", "edit", "export", "import"],
    )
    p_rules.add_argument("path", nargs="?", help="test 时是待测文件；export/import 时是 JSON 路径")
    p_rules.set_defaults(func=cmd_rules)

    p_gui = sub.add_parser("gui", help="打开本地图形界面")
    p_gui.add_argument("--port", type=int, default=8765)
    p_gui.add_argument("--no-browser", action="store_true")
    p_gui.set_defaults(func=cmd_gui)

    p_status = sub.add_parser("status", help="查看当前状态")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="查看 / 修改配置")
    p_config.add_argument("action", nargs="?", default="show", choices=["show", "set"])
    p_config.add_argument("key", nargs="?")
    p_config.add_argument("value", nargs="?")
    p_config.add_argument("--append", action="store_true", help="追加到列表而不是覆盖")
    p_config.set_defaults(func=cmd_config)

    return parser


def _setup_stdout() -> None:
    """只兜住编码错误，不改变平台默认编码。

    Windows 中文环境下 stdout 默认 cp936，PowerShell 管道也按这个编码解码；
    强行改成 UTF-8 会让 PowerShell 二次转码，反而显示乱码。
    """
    stream = sys.stdout
    if not hasattr(stream, "reconfigure"):
        return
    try:
        stream.reconfigure(errors="replace")
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
