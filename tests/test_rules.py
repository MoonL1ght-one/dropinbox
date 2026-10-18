"""规则引擎测试：只关心判定结果是否正确。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dropinbox.meta import SourceInfo, domain_of
from dropinbox.rules import (
    FileContext,
    Rule,
    build_context,
    category_of,
    evaluate,
    parse_duration,
    parse_size,
    rules_sorted,
)


def ctx_for(name: str, size: int = 100, age: float = 3600, now: float | None = None) -> FileContext:
    current = now if now is not None else time.time()
    return FileContext(path=Path("/tmp") / name, size=size, mtime=current - age, now=current)


class TestUnits(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("10KB"), 10 * 1024)
        self.assertEqual(parse_size("1.5MB"), int(1.5 * 1024 * 1024))
        self.assertEqual(parse_size("2 GB"), 2 * 1024 ** 3)
        self.assertEqual(parse_size(2048), 2048)
        self.assertEqual(parse_size("512"), 512)

    def test_parse_size_invalid(self):
        with self.assertRaises(ValueError):
            parse_size("abc KB")

    def test_parse_duration(self):
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("7d"), 7 * 86400)

    def test_domain_of(self):
        self.assertEqual(domain_of("https://www.github.com/a/b"), "github.com")
        self.assertEqual(domain_of("item.jd.com/x"), "item.jd.com")
        self.assertEqual(domain_of(""), "")

    def test_category(self):
        self.assertEqual(category_of(Path("a.PNG")), "images")
        self.assertEqual(category_of(Path("a.xlsx")), "spreadsheets")
        self.assertIsNone(category_of(Path("a.unknownext")))


class TestMatching(unittest.TestCase):
    def test_ext_match(self):
        rule = Rule.from_dict({"id": "r", "name": "r", "target": "x", "match": {"ext": ["pdf"]}})
        self.assertTrue(evaluate(rule, ctx_for("a.pdf"))[0])
        self.assertFalse(evaluate(rule, ctx_for("a.docx"))[0])

    def test_category_match(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"categories": ["images"]}}
        )
        self.assertTrue(evaluate(rule, ctx_for("photo.jpg"))[0])
        self.assertFalse(evaluate(rule, ctx_for("doc.pdf"))[0])

    def test_name_regex(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"name_regex": r"(?i)invoice"}}
        )
        self.assertTrue(evaluate(rule, ctx_for("INVOICE_2026.pdf"))[0])
        self.assertFalse(evaluate(rule, ctx_for("report.pdf"))[0])

    def test_invalid_regex_does_not_crash(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"name_regex": "([unclosed"}}
        )
        ok, reason = evaluate(rule, ctx_for("a.pdf"))
        self.assertFalse(ok)
        self.assertIn("正则", reason)

    def test_size_range(self):
        rule = Rule.from_dict(
            {
                "id": "r",
                "name": "r",
                "target": "x",
                "match": {"size_min": "1MB", "size_max": "10MB"},
            }
        )
        self.assertTrue(evaluate(rule, ctx_for("a.zip", size=2 * 1024 ** 2))[0])
        self.assertFalse(evaluate(rule, ctx_for("a.zip", size=100))[0])
        self.assertFalse(evaluate(rule, ctx_for("a.zip", size=50 * 1024 ** 2))[0])

    def test_age(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"older_than": "2h"}}
        )
        self.assertTrue(evaluate(rule, ctx_for("a.txt", age=3 * 3600))[0])
        self.assertFalse(evaluate(rule, ctx_for("a.txt", age=600))[0])

    def test_source_domain(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"source_domains": ["github.com"]}}
        )
        ctx = ctx_for("repo.zip")
        ctx._source = SourceInfo(url="https://github.com/a/b/archive.zip")
        ctx._source_loaded = True
        self.assertTrue(evaluate(rule, ctx)[0])

        ctx2 = ctx_for("repo.zip")
        ctx2._source = SourceInfo(url="https://gitlab.com/a/b.zip")
        ctx2._source_loaded = True
        self.assertFalse(evaluate(rule, ctx2)[0])

    def test_missing_source_is_not_a_match(self):
        rule = Rule.from_dict(
            {"id": "r", "name": "r", "target": "x", "match": {"source_domains": ["github.com"]}}
        )
        ctx = ctx_for("a.zip")
        ctx._source = None
        ctx._source_loaded = True
        ok, reason = evaluate(rule, ctx)
        self.assertFalse(ok)
        self.assertIn("无来源", reason)

    def test_match_mode_all(self):
        rule = Rule.from_dict(
            {
                "id": "r",
                "name": "r",
                "target": "x",
                "match_mode": "all",
                "match": {"ext": ["pdf"], "name_regex": "invoice"},
            }
        )
        self.assertTrue(evaluate(rule, ctx_for("invoice.pdf"))[0])
        self.assertFalse(evaluate(rule, ctx_for("invoice.docx"))[0])

    def test_match_mode_any(self):
        rule = Rule.from_dict(
            {
                "id": "r",
                "name": "r",
                "target": "x",
                "match_mode": "any",
                "match": {"ext": ["pdf"], "name_regex": "invoice"},
            }
        )
        self.assertTrue(evaluate(rule, ctx_for("invoice.docx"))[0])
        self.assertTrue(evaluate(rule, ctx_for("x.pdf"))[0])
        self.assertFalse(evaluate(rule, ctx_for("other.docx"))[0])

    def test_empty_match_is_catch_all(self):
        rule = Rule.from_dict({"id": "r", "name": "r", "target": "x", "match": {}})
        ok, reason = evaluate(rule, ctx_for("anything.bin"))
        self.assertTrue(ok)
        self.assertIn("兜底", reason)

    def test_rules_sorted_and_disabled_filtered(self):
        rules = [
            {"id": "b", "name": "b", "target": "x", "priority": 50, "enabled": True},
            {"id": "a", "name": "a", "target": "x", "priority": 10, "enabled": True},
            {"id": "c", "name": "c", "target": "x", "priority": 1, "enabled": False},
        ]
        ordered = rules_sorted(rules)
        self.assertEqual([r.id for r in ordered], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
