"""操作历史与撤销。

每次"应用"都会形成一个 batch，batch 里是逐条 (src -> dst) 的搬运记录。
撤销 = 逆序把 dst 搬回 src，并把记录标记为 undone。文件已经被删掉、
或者原位置已有同名新文件时，都能安全退化处理，绝不覆盖用户数据。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import db_path

__all__ = [
    "Batch",
    "Operation",
    "init_db",
    "create_batch",
    "record_operation",
    "list_batches",
    "batch_operations",
    "last_undoable_batch",
    "undo_batch",
    "stats",
    "prune",
]

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'manual',
    status      TEXT    NOT NULL DEFAULT 'applied',
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    src         TEXT    NOT NULL,
    dst         TEXT    NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    rule_id     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    undone      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ops_batch ON operations(batch_id);
CREATE INDEX IF NOT EXISTS idx_ops_src   ON operations(src);
"""


@dataclass
class Batch:
    id: int
    created_at: str
    kind: str
    status: str
    note: str
    count: int = 0
    bytes: int = 0


@dataclass
class Operation:
    id: int
    batch_id: int
    src: Path
    dst: Path
    size: int
    rule_id: str
    created_at: str
    undone: bool


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _cursor(path: Optional[Path] = None):
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Optional[Path] = None) -> Path:
    with _cursor(path) as conn:
        conn.executescript(SCHEMA)
    return path or db_path()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_batch(kind: str = "manual", note: str = "", path: Optional[Path] = None) -> int:
    with _cursor(path) as conn:
        cur = conn.execute(
            "INSERT INTO batches (created_at, kind, status, note) VALUES (?, ?, 'applied', ?)",
            (_now(), kind, note),
        )
        return int(cur.lastrowid)


def record_operation(
    batch_id: int,
    src: Path,
    dst: Path,
    size: int = 0,
    rule_id: str = "",
    path: Optional[Path] = None,
) -> int:
    with _cursor(path) as conn:
        cur = conn.execute(
            "INSERT INTO operations (batch_id, src, dst, size, rule_id, created_at, undone)"
            " VALUES (?, ?, ?, ?, ?, ?, 0)",
            (batch_id, str(src), str(dst), int(size), rule_id, _now()),
        )
        return int(cur.lastrowid)


def list_batches(limit: int = 50, path: Optional[Path] = None) -> List[Batch]:
    with _cursor(path) as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.created_at, b.kind, b.status, b.note,
                   COUNT(o.id) AS cnt,
                   COALESCE(SUM(o.size), 0) AS total
            FROM batches b
            LEFT JOIN operations o ON o.batch_id = b.id AND o.undone = 0
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        Batch(
            id=int(r["id"]),
            created_at=r["created_at"],
            kind=r["kind"],
            status=r["status"],
            note=r["note"],
            count=int(r["cnt"] or 0),
            bytes=int(r["total"] or 0),
        )
        for r in rows
    ]


def batch_operations(batch_id: int, path: Optional[Path] = None) -> List[Operation]:
    with _cursor(path) as conn:
        rows = conn.execute(
            "SELECT * FROM operations WHERE batch_id = ? ORDER BY id ASC", (batch_id,)
        ).fetchall()
    return [
        Operation(
            id=int(r["id"]),
            batch_id=int(r["batch_id"]),
            src=Path(r["src"]),
            dst=Path(r["dst"]),
            size=int(r["size"]),
            rule_id=r["rule_id"],
            created_at=r["created_at"],
            undone=bool(r["undone"]),
        )
        for r in rows
    ]


def last_undoable_batch(path: Optional[Path] = None) -> Optional[int]:
    with _cursor(path) as conn:
        row = conn.execute(
            """
            SELECT b.id FROM batches b
            JOIN operations o ON o.batch_id = b.id
            WHERE o.undone = 0 AND b.status IN ('applied', 'partial')
            ORDER BY b.id DESC
            LIMIT 1
            """
        ).fetchone()
    return int(row["id"]) if row else None


def _free_path(path: Path) -> Path:
    """如果目标已存在，追加 (1)(2)... 绝不覆盖。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {path} 找到可用文件名")


def _prune_empty_upwards(directory: Path, stop_at: Optional[Path] = None) -> None:
    """从 directory 起向上删空目录，遇到非空或到 stop_at 就停。"""
    current = directory
    while True:
        if stop_at is not None and current == stop_at:
            return
        try:
            if not current.is_dir() or any(current.iterdir()):
                return
            current.rmdir()
        except OSError:
            return
        if current.parent == current:
            return
        current = current.parent


def undo_batch(batch_id: int, path: Optional[Path] = None) -> Dict[str, object]:
    """撤销一个批次。返回 {restored, skipped, missing, errors, status}。"""
    ops = [op for op in batch_operations(batch_id, path) if not op.undone]
    if not ops:
        return {"restored": 0, "skipped": 0, "missing": [], "errors": [], "status": "empty"}

    restored = 0
    skipped = 0
    missing: List[str] = []
    errors: List[str] = []
    done_ids: List[int] = []

    # 逆序恢复，尽量还原"后进先出"的原始状态
    for op in reversed(ops):
        dst = op.dst
        if not dst.exists():
            missing.append(str(dst))
            skipped += 1
            done_ids.append(op.id)
            continue
        try:
            target = _free_path(op.src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(target))
            restored += 1
            done_ids.append(op.id)
        except OSError as exc:
            errors.append(f"{dst}: {exc}")
            skipped += 1

    if done_ids:
        with _cursor(path) as conn:
            conn.executemany(
                "UPDATE operations SET undone = 1 WHERE id = ?",
                [(i,) for i in done_ids],
            )
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM operations WHERE batch_id = ? AND undone = 0",
                (batch_id,),
            ).fetchone()["c"]
            status = "undone" if remaining == 0 else "partial"
            conn.execute("UPDATE batches SET status = ? WHERE id = ?", (status, batch_id))
    else:
        status = "applied"

    # 顺手清理搬空了的归档目录：按深度倒序逐个尝试，非空目录自然失败
    # 用所有目标目录的公共父级作为下界，避免一路删到用户的下载/主目录
    candidates = {op.dst.parent for op in ops}
    stop_at: Optional[Path] = None
    try:
        stop_at = Path(os.path.commonpath([str(p) for p in candidates]))
    except ValueError:
        stop_at = None
    for directory in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        _prune_empty_upwards(directory, stop_at=stop_at)

    return {
        "restored": restored,
        "skipped": skipped,
        "missing": missing,
        "errors": errors,
        "status": status,
    }


def stats(path: Optional[Path] = None) -> Dict[str, int]:
    with _cursor(path) as conn:
        moved = conn.execute(
            "SELECT COUNT(*) AS c FROM operations WHERE undone = 0"
        ).fetchone()["c"]
        total = conn.execute("SELECT COUNT(*) AS c FROM operations").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    return {"files_sorted": int(moved), "operations_total": int(total), "batches": int(batches)}


def prune(keep: int = 200, path: Optional[Path] = None) -> int:
    """只保留最近 keep 个批次，避免数据库无限增长。"""
    removed = 0
    with _cursor(path) as conn:
        rows = conn.execute(
            "SELECT id FROM batches ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,)
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM operations WHERE batch_id = ?", (r["id"],))
            conn.execute("DELETE FROM batches WHERE id = ?", (r["id"],))
            removed += 1
    return removed
