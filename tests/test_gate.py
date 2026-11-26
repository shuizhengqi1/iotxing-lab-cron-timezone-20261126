#!/usr/bin/env python3
"""Unit tests for the cron timezone gate and next-run arithmetic."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import cron_timezone_gate as gate  # noqa: E402

FROZEN = datetime(2026, 11, 26, 8, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
UTC = timezone.utc
SH = ZoneInfo("Asia/Shanghai")
FT = ZoneInfo("America/Fortaleza")
NY = ZoneInfo("America/New_York")
NOW = "--now=2026-11-26T08:00:00+08:00"


class NextRunTests(unittest.TestCase):
    def test_naive_utc_nine_is_seventeen_shanghai(self):
        fires = gate.next_cron_fires("0 9 * * *", ZoneInfo("UTC"), FROZEN, 3)
        self.assertEqual(fires[0], datetime(2026, 11, 26, 9, 0, tzinfo=UTC))
        self.assertEqual(fires[0].astimezone(SH).strftime("%Y-%m-%d %H:%M %z"), "2026-11-26 17:00 +0800")
        self.assertEqual(fires[0].astimezone(FT).strftime("%Y-%m-%d %H:%M %z"), "2026-11-26 06:00 -0300")
        self.assertEqual(
            [f.astimezone(UTC) for f in fires],
            [
                datetime(2026, 11, 26, 9, 0, tzinfo=UTC),
                datetime(2026, 11, 27, 9, 0, tzinfo=UTC),
                datetime(2026, 11, 28, 9, 0, tzinfo=UTC),
            ],
        )

    def test_intended_nine_shanghai_is_previous_calendar_day_in_fortaleza(self):
        fires = gate.next_cron_fires("0 9 * * *", SH, FROZEN, 3)
        self.assertEqual(fires[0].astimezone(UTC), datetime(2026, 11, 26, 1, 0, tzinfo=UTC))
        self.assertEqual(fires[0].astimezone(SH).strftime("%Y-%m-%d %H:%M %z"), "2026-11-26 09:00 +0800")
        ft = fires[0].astimezone(FT)
        self.assertEqual(ft.strftime("%Y-%m-%d %H:%M %z"), "2026-11-25 22:00 -0300")
        self.assertEqual(ft.date().isoformat(), "2026-11-25")

    def test_utc_converted_one_matches_intended_at_this_instant(self):
        converted = gate.next_cron_fires("0 1 * * *", ZoneInfo("UTC"), FROZEN, 1)[0]
        intended = gate.next_cron_fires("0 9 * * *", SH, FROZEN, 1)[0]
        self.assertEqual(converted, intended)

    def test_shanghai_and_fortaleza_have_no_dst_in_2026(self):
        for month in (1, 3, 7, 11):
            sh = datetime(2026, month, 15, 12, 0, tzinfo=SH)
            ft = datetime(2026, month, 15, 12, 0, tzinfo=FT)
            self.assertEqual(sh.utcoffset().total_seconds(), 8 * 3600, month)
            self.assertEqual(ft.utcoffset().total_seconds(), -3 * 3600, month)

    def test_us_spring_forward_2026_two_thirty_advances_to_three(self):
        self.assertEqual(datetime(2026, 3, 8).strftime("%A"), "Sunday")
        now = datetime(2026, 3, 8, 0, 0, tzinfo=NY)
        fires = gate.next_cron_fires("30 2 * * *", NY, now, 3)
        first = fires[0].astimezone(NY)
        self.assertEqual(first.strftime("%Y-%m-%d %H:%M %z"), "2026-03-08 03:00 -0400")
        self.assertEqual(fires[0].astimezone(UTC), datetime(2026, 3, 8, 7, 0, tzinfo=UTC))
        # The following days are ordinary 02:30 EDT.
        self.assertEqual(fires[1].astimezone(NY).strftime("%Y-%m-%d %H:%M %z"), "2026-03-09 02:30 -0400")
        self.assertEqual(fires[2].astimezone(NY).strftime("%Y-%m-%d %H:%M %z"), "2026-03-10 02:30 -0400")


class ParserAndGateTests(unittest.TestCase):
    def test_parse_naive_missing_timezone_keeps_comment(self):
        entries = gate.parse_schedule_entries(ROOT / "examples" / "naive.yml")
        self.assertIsNotNone(entries)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].cron, "0 9 * * *")
        self.assertIsNone(entries[0].timezone_name)
        self.assertTrue(any("9 点" in c for c in entries[0].comments))

    def test_parse_timezone_fix(self):
        entries = gate.parse_schedule_entries(ROOT / "examples" / "timezone-fix.yml")
        self.assertEqual(entries[0].cron, "0 9 * * *")
        self.assertEqual(entries[0].timezone_name, "Asia/Shanghai")

    def test_no_schedule_returns_none(self):
        self.assertIsNone(gate.parse_schedule_entries(ROOT / "examples" / "no-schedule.yml"))

    def test_gate_refuses_naive(self):
        code = gate.main(["--path", str(ROOT / "examples" / "naive.yml"), NOW, "--next", "3"])
        self.assertEqual(code, 1)

    def test_gate_refuses_utc_converted(self):
        code = gate.main(["--path", str(ROOT / "examples" / "utc-converted.yml"), NOW])
        self.assertEqual(code, 1)

    def test_gate_allows_timezone_fix(self):
        code = gate.main(["--path", str(ROOT / "examples" / "timezone-fix.yml"), NOW])
        self.assertEqual(code, 0)

    def test_gate_ignores_workflow_without_schedule(self):
        code = gate.main(["--path", str(ROOT / "examples" / "no-schedule.yml"), NOW])
        self.assertEqual(code, 0)

    def test_gate_refuses_comment_mismatch_even_with_timezone(self):
        code = gate.main(["--path", str(ROOT / "examples" / "comment-mismatch.yml"), NOW])
        self.assertEqual(code, 1)

    def test_comment_without_zone_does_not_false_fail_timezone_fix(self):
        # timezone-fix.yml has no time+zone comment; it must still ALLOW.
        entries = gate.parse_schedule_entries(ROOT / "examples" / "timezone-fix.yml")
        self.assertIsNone(gate.extract_comment_intent(entries[0].comments))


class HourCopyTests(unittest.TestCase):
    def test_overwrites_utc_converted_hour_back_to_nine(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import hour_copy

        ticket = (ROOT / "TICKET.md").read_text(encoding="utf-8")
        hour, minute = hour_copy.ticket_clock(ticket)
        self.assertEqual((hour, minute), (9, 0))
        text = (ROOT / "examples" / "utc-converted.yml").read_text(encoding="utf-8")
        patched, n = hour_copy.patch_text(text, hour, minute)
        self.assertEqual(n, 1)
        self.assertIn("cron: '0 9 * * *'", patched)
        self.assertNotIn("cron: '0 1 * * *'", patched)
        # Header comments may mention the word timezone; the YAML key itself
        # is still absent, which is the point of this helper.
        import re
        self.assertIsNone(re.search(r"^\s*timezone:", patched, re.M))


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
