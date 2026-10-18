"""归档、撤销、重名保护的行为测试。

这些用例是项目最该被信任的部分：撤销必须不丢文件、不覆盖已有文件。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dropinbox import config as cfgmod
from dropinbox import history
from dropinbox.organizer import apply_plan, collect_files, is_inside, plan, render_target
from dropinbox.rules import FileContext

RULES = [
    {
        "id": "invoice",
        "name": "发票",
        "enabled": True,
        "priority": 10,
        "match_mode": "any",
        "match": {"name_regex": "发票"},
        "target": "财务/发票",
    },
    {
        "id": "images",
        "name": "图片",
        "enabled": True,
        "priority": 100,
        "match": {"categories": ["images"]},
        "target": "图片",
    },
    {
        "id": "fallback",
        "name": "兜底",
        "enabled": True,
        "priority": 9999,
        "match": {},
        "target": "未分类",
    },
]


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dropinbox-test-"))
        self._old_home = os.environ.get("DROPINBOX_HOME")
        os.environ["DROPINBOX_HOME"] = str(self.tmp / "home")
        self.dl = self.tmp / "downloads"
        self.dl.mkdir()
        self.root = self.dl / "_Inbox"
        self.cfg = dict(cfgmod.DEFAULT_CONFIG)
        self.cfg["watch_dirs"] = [str(self.dl)]
        self.cfg["archive_root"] = str(self.root)
        self.cfg["min_age_seconds"] = 0
        history.init_db()

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("DROPINBOX_HOME", None)
        else:
            os.environ["DROPINBOX_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_file(self, name: str, content: str = "x", age: float = 3600) -> Path:
        path = self.dl / name
        path.write_text(content, encoding="utf-8")
        past = time.time() - age
        os.utime(path, (past, past))
        return path


class TestCollect(BaseCase):
    def test_ignores_partial_downloads(self):
        self.make_file("good.pdf")
        self.make_file("half.pdf.crdownload")
        self.make_file("temp.tmp")
        found = {p.name for p in collect_files(self.cfg)}
        self.assertIn("good.pdf", found)
        self.assertNotIn("half.pdf.crdownload", found)
        self.assertNotIn("temp.tmp", found)

    def test_skips_archive_root(self):
        self.root.mkdir(parents=True)
        (self.root / "inner.pdf").write_text("x", encoding="utf-8")
        self.make_file("outer.pdf")
        found = {p.name for p in collect_files(self.cfg)}
        self.assertEqual(found, {"outer.pdf"})

    def test_is_inside(self):
        self.assertTrue(is_inside(self.dl / "a" / "b.txt", self.dl))
        self.assertFalse(is_inside(self.tmp, self.dl))


class TestPlan(BaseCase):
    def test_priority_wins(self):
        self.make_file("发票_2026.png")  # 同时命中 发票 和 图片
        decisions = plan(self.cfg, RULES)
        hit = [d for d in decisions if d.path.name == "发票_2026.png"][0]
        self.assertEqual(hit.rule_id, "invoice")
        self.assertEqual(hit.target, self.root / "财务" / "发票")

    def test_fallback_catches_unknown(self):
        self.make_file("mystery.xyz")
        decisions = plan(self.cfg, RULES)
        hit = [d for d in decisions if d.path.name == "mystery.xyz"][0]
        self.assertEqual(hit.rule_id, "fallback")

    def test_fresh_file_waits(self):
        self.cfg["min_age_seconds"] = 60
        self.make_file("fresh.pdf", age=1)
        decisions = plan(self.cfg, RULES)
        self.assertEqual(decisions[0].action, "skip")
        self.assertIn("等待", decisions[0].reason)


class TestTargetTemplate(BaseCase):
    def test_placeholders(self):
        path = self.make_file("报告.docx")
        ctx = FileContext(path=path, size=10, mtime=time.mktime((2026, 3, 5, 12, 0, 0, 0, 0, -1)), now=time.time())
        target = render_target("文档/{year}/{month}", ctx, self.root)
        self.assertEqual(target, self.root / "文档" / "2026" / "03")

    def test_unknown_placeholder_is_empty(self):
        path = self.make_file("a.txt")
        ctx = FileContext(path=path, size=1, mtime=time.time(), now=time.time())
        target = render_target("x/{nope}/y", ctx, self.root)
        self.assertEqual(target, self.root / "x" / "y")

    def test_illegal_chars_sanitized(self):
        path = self.make_file("a.txt")
        ctx = FileContext(path=path, size=1, mtime=time.time(), now=time.time())
        target = render_target('bad:name*here', ctx, self.root)
        self.assertNotIn(":", target.name)
        self.assertNotIn("*", target.name)


class TestApplyAndUndo(BaseCase):
    def test_apply_keeps_filename_and_extension(self):
        self.make_file("photo.jpg", "img")
        decisions = plan(self.cfg, RULES)
        result = apply_plan(decisions, kind="test")
        self.assertEqual(result["moved"], 1)
        moved = self.root / "图片" / "photo.jpg"
        self.assertTrue(moved.exists(), "文件必须保留原名和扩展名")
        self.assertFalse((self.dl / "photo.jpg").exists())

    def test_undo_restores_everything(self):
        self.make_file("photo.jpg")
        self.make_file("发票.pdf")
        apply_plan(plan(self.cfg, RULES), kind="test")
        batch = history.last_undoable_batch()
        self.assertIsNotNone(batch)

        result = history.undo_batch(batch)
        self.assertEqual(result["restored"], 2)
        self.assertTrue((self.dl / "photo.jpg").exists())
        self.assertTrue((self.dl / "发票.pdf").exists())
        self.assertEqual(history.stats()["files_sorted"], 0)

    def test_undo_does_not_overwrite_new_file(self):
        self.make_file("report.pdf", "old")
        apply_plan(plan(self.cfg, RULES), kind="test")
        # 用户又下载了一个同名文件
        self.make_file("report.pdf", "new")
        history.undo_batch(history.last_undoable_batch())

        names = sorted(p.name for p in self.dl.iterdir() if p.is_file())
        self.assertEqual(names, ["report (1).pdf", "report.pdf"])
        self.assertEqual((self.dl / "report.pdf").read_text(encoding="utf-8"), "new")

    def test_undo_when_archive_file_deleted(self):
        self.make_file("gone.pdf")
        apply_plan(plan(self.cfg, RULES), kind="test")
        moved = self.root / "未分类" / "gone.pdf"
        moved.unlink()
        result = history.undo_batch(history.last_undoable_batch())
        self.assertEqual(result["restored"], 0)
        self.assertEqual(len(result["missing"]), 1)

    def test_double_undo_is_safe(self):
        self.make_file("a.pdf")
        apply_plan(plan(self.cfg, RULES), kind="test")
        batch = history.last_undoable_batch()
        history.undo_batch(batch)
        second = history.undo_batch(batch)
        self.assertEqual(second["status"], "empty")

    def test_name_collision_in_archive(self):
        first = self.make_file("dup.pdf", "one")
        apply_plan(plan(self.cfg, RULES, paths=[first]), kind="test")
        second = self.make_file("dup.pdf", "two")
        apply_plan(plan(self.cfg, RULES, paths=[second]), kind="test")

        target_dir = self.root / "未分类"
        names = sorted(p.name for p in target_dir.iterdir())
        self.assertEqual(names, ["dup (1).pdf", "dup.pdf"])

    def test_already_in_place_is_not_moved(self):
        target_dir = self.root / "未分类"
        target_dir.mkdir(parents=True)
        rule = {"id": "r", "name": "r", "enabled": True, "priority": 1,
                "match": {"ext": ["pdf"]}, "target": "未分类"}
        path = target_dir / "here.pdf"
        path.write_text("x", encoding="utf-8")
        decisions = plan(self.cfg, [rule], paths=[path])
        self.assertEqual(decisions[0].action, "skip")
        self.assertIn("无需移动", decisions[0].reason)


if __name__ == "__main__":
    unittest.main()
