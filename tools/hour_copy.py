#!/usr/bin/env python3
"""Copy a local clock hour from a ticket into a workflow cron hour field.

This is a deterministic helper that demonstrates a failure mode, not a
tool to run in production. It looks for the first 'N 点' (or 'N:MM') in
the ticket and overwrites the hour (and minute, if present) of every
cron: under on.schedule. It does not add a timezone: field.

A reviewer who already converted 09:00 Asia/Shanghai to '0 1 * * *'
UTC will see that conversion undone the next time this helper runs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DIAN = re.compile(r"(?:上午|下午)?\s*([0-2]?\d)\s*点")
HM = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
CRON_LINE = re.compile(
    r"^(\s*(?:-\s*)?cron:\s*)(['\"]?)([^'\"#\n]+)(['\"]?)(.*)$"
)


def ticket_clock(text: str) -> tuple[int, int]:
    m = DIAN.search(text)
    if m:
        hour = int(m.group(1))
        if "下午" in m.group(0) and hour < 12:
            hour += 12
        return hour, 0
    m = HM.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    raise SystemExit("hour_copy: no clock time found in ticket")


def patch_text(text: str, hour: int, minute: int) -> tuple[str, int]:
    out = []
    n = 0
    for line in text.splitlines(keepends=True):
        # Keep newline off for matching, then restore.
        nl = ""
        body = line
        if line.endswith("\r\n"):
            body, nl = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, nl = line[:-1], "\n"
        m = CRON_LINE.match(body)
        if not m:
            out.append(line)
            continue
        prefix, q1, expr, q2, rest = m.groups()
        fields = expr.strip().split()
        if len(fields) != 5:
            out.append(line)
            continue
        fields[0] = str(minute)
        fields[1] = str(hour)
        new = prefix + q1 + " ".join(fields) + q2 + rest + nl
        out.append(new)
        n += 1
    return "".join(out), n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ticket", required=True)
    p.add_argument("--workflow", required=True)
    p.add_argument("--write", action="store_true", help="overwrite the workflow in place")
    args = p.parse_args(argv)
    ticket = Path(args.ticket).read_text(encoding="utf-8")
    hour, minute = ticket_clock(ticket)
    path = Path(args.workflow)
    original = path.read_text(encoding="utf-8")
    patched, n = patch_text(original, hour, minute)
    print("hour_copy: ticket clock is %02d:%02d (local hour copied into cron; timezone not written)" % (hour, minute))
    print("hour_copy: patched %d cron line(s) in %s" % (n, path))
    if not args.write:
        sys.stdout.write(patched)
        return 0 if n else 1
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        print("hour_copy: wrote %s" % path)
    else:
        print("hour_copy: no change")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
