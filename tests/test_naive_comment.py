#!/usr/bin/env python3
"""False-green checks for the naive workflow.

These tests only ask: does the YAML still look like a five-field cron,
and does the comment still contain '9 点'? They pass on the naive
fixture that will fire at 17:00 Asia/Shanghai. They are the approach
that failed, kept here to record that result.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAIVE = ROOT / "examples" / "naive.yml"


def _split_comment(raw: str) -> tuple[str, str | None]:
    in_single = False
    in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return raw[:i].rstrip(), raw[i + 1 :].strip()
    return raw.rstrip(), None


class NaiveCommentTests(unittest.TestCase):
    def setUp(self):
        self.text = NAIVE.read_text(encoding="utf-8")

    def test_file_exists_and_has_on_schedule(self):
        self.assertTrue(NAIVE.is_file())
        self.assertIn("schedule:", self.text)
        self.assertIn("cron:", self.text)

    def test_cron_has_five_fields(self):
        match = re.search(r"cron:\s*['\"]([^'\"]+)['\"]", self.text)
        self.assertIsNotNone(match, "cron: line not found")
        fields = match.group(1).split()
        self.assertEqual(len(fields), 5, fields)

    def test_comment_contains_nine(self):
        found = False
        for line in self.text.splitlines():
            _, comment = _split_comment(line)
            if comment and "9 点" in comment:
                found = True
        self.assertTrue(found, "expected a comment containing '9 点'")


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
