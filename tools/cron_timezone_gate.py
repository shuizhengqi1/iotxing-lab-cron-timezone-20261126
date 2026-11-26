#!/usr/bin/env python3
"""Fail-closed gate for GitHub Actions scheduled workflows.

Every on.schedule entry must declare a valid IANA timezone:.
The default is still UTC when the field is omitted. A comment that
repeats the same hour number as the cron is not a substitute.

This script computes next fire times with zoneinfo. It does not ask
GitHub whether a scheduled workflow has already run, and it does not
claim a live Actions schedule fired at those instants.

Usage:
  python tools/cron_timezone_gate.py
  python tools/cron_timezone_gate.py --workflows-dir .github/workflows
  python tools/cron_timezone_gate.py --path examples/naive.yml
  python tools/cron_timezone_gate.py --now 2026-11-26T08:00:00+08:00 --next 3
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DEFAULT_SHOW_ZONES = ("UTC", "Asia/Shanghai", "America/Fortaleza")
IANA_IN_TEXT = re.compile(r"\b(?:UTC|GMT|[A-Za-z]+/[A-Za-z0-9_+-]+)\b")
# Chinese city aliases only when they unambiguously point at a zone we care about.
CITY_ALIASES = {
    "上海": "Asia/Shanghai",
    "北京": "Asia/Shanghai",  # same offset, China has one civil zone
    "纽约": "America/New_York",
}
CLOCK_HM = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
CLOCK_DIAN = re.compile(r"(?:上午|下午)?\s*([0-2]?\d)\s*点")


@dataclass
class ScheduleEntry:
    path: Path
    line: int
    cron: str | None
    timezone_name: str | None
    comments: list[str] = field(default_factory=list)
    parse_error: str | None = None


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


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


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_cron_fields(expr: str) -> list[str]:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron must have exactly 5 fields, got %d in %r" % (len(fields), expr))
    return fields


def _parse_field(field: str, minimum: int, maximum: int, names: dict[str, int] | None = None) -> set[int]:
    if names:
        def tok(t: str) -> int:
            up = t.upper()
            if up in names:
                return names[up]
            return int(t)
    else:
        def tok(t: str) -> int:
            return int(t)

    out: set[int] = set()
    for part in field.split(","):
        step = 1
        body = part
        if "/" in part:
            body, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("invalid step %r in %r" % (step_s, field))
        if body in {"*", "?"}:
            start, end = minimum, maximum
        elif "-" in body:
            a, b = body.split("-", 1)
            start, end = tok(a), tok(b)
        else:
            start = end = tok(body)
        if start > end or start < minimum or end > maximum:
            # cron Sunday: allow 7 as alias of 0 in day-of-week
            if names and start == 7 and end == 7 and maximum == 6:
                out.add(0)
                continue
            raise ValueError("field %r out of range %d-%d" % (part, minimum, end if False else maximum))
        for v in range(start, end + 1, step):
            if names and v == 7 and maximum == 6:
                out.add(0)
            else:
                out.add(v)
    return out


DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
MON_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class Cron:
    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]
    dom_star: bool
    dow_star: bool
    expr: str

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        minute, hour, dom, month, dow = parse_cron_fields(expr)
        return cls(
            minutes=_parse_field(minute, 0, 59),
            hours=_parse_field(hour, 0, 23),
            doms=_parse_field(dom, 1, 31),
            months=_parse_field(month, 1, 12, MON_NAMES),
            dows=_parse_field(dow, 0, 6, DOW_NAMES),
            dom_star=dom == "*",
            dow_star=dow == "*",
            expr=expr,
        )

    def day_matches(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        # POSIX: if both DOM and DOW are restricted, fire when either matches.
        cron_dow = (d.weekday() + 1) % 7  # Monday=0 in weekday(); cron Sunday=0
        dom_ok = d.day in self.doms
        dow_ok = cron_dow in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok


def is_nonexistent(naive: datetime, tz: ZoneInfo) -> bool:
    aware = naive.replace(tzinfo=tz, fold=0)
    back = aware.astimezone(UTC).astimezone(tz)
    return back.replace(tzinfo=None) != naive


def first_valid_local(naive: datetime, tz: ZoneInfo) -> datetime:
    """Advance a skipped DST wall time to the next valid local time.

    GitHub's docs (workflow syntax, on.schedule): during DST spring-forward,
    schedules in skipped hours advance to the next valid time. Example:
    a 2:30 AM schedule advances to 3:00 AM.
    """
    t = naive.replace(second=0, microsecond=0)
    for _ in range(180):
        if not is_nonexistent(t, tz):
            return t
        t += timedelta(minutes=1)
    raise ValueError("no valid local time after %s in %s" % (naive, tz))


def to_aware(naive_valid: datetime, tz: ZoneInfo) -> datetime:
    return naive_valid.replace(tzinfo=tz, fold=0)


def next_cron_fires(expr: str, tz: ZoneInfo, now: datetime, count: int = 3) -> list[datetime]:
    cron = Cron.parse(expr)
    now = now.astimezone(tz)
    # Walk local calendar days. Start at today's date.
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found: list[datetime] = []
    for _ in range(366 * 3):
        naive_day = day.replace(tzinfo=None)
        if cron.day_matches(naive_day):
            for hour in sorted(cron.hours):
                for minute in sorted(cron.minutes):
                    naive = naive_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if is_nonexistent(naive, tz):
                        # Official rule: fire at the next valid local time.
                        # That instant may already have been scheduled; skip dupes.
                        naive = first_valid_local(naive, tz)
                    aware = to_aware(naive, tz)
                    if aware <= now:
                        continue
                    if found and aware == found[-1]:
                        continue
                    found.append(aware)
                    if len(found) >= count:
                        return found
        day += timedelta(days=1)
        day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    raise RuntimeError("could not find %d fire times for %r in %s after %s" % (count, expr, tz, now))


def format_instant(dt: datetime, zones: tuple[str, ...] = DEFAULT_SHOW_ZONES) -> str:
    parts = []
    for name in zones:
        z = UTC if name in {"UTC", "GMT"} else ZoneInfo(name)
        local = dt.astimezone(z)
        if name in {"UTC", "GMT"}:
            parts.append(local.strftime("%Y-%m-%d %H:%M:%S") + "Z")
        else:
            parts.append(local.strftime("%Y-%m-%d %H:%M:%S%z") + " " + name)
    return " | ".join(parts)


def load_zone(name: str) -> ZoneInfo:
    if name in {"UTC", "GMT"}:
        # ZoneInfo("UTC") works, but keep a stable object.
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("not a valid IANA timezone: %r" % name) from exc


def parse_schedule_entries(path: Path, text: str | None = None) -> list[ScheduleEntry] | None:
    """Return schedule entries, or None if the workflow has no on.schedule key."""
    if text is None:
        text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_on = False
    on_indent = None
    in_schedule = False
    schedule_indent = None
    schedule_seen = False
    current: ScheduleEntry | None = None
    pending_comments: list[str] = []
    entries: list[ScheduleEntry] = []

    def close():
        nonlocal current
        if current is not None:
            entries.append(current)
            current = None

    for idx, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        indent = _indent(raw) if not raw.startswith("\t") else 8
        stripped = raw.strip()
        if stripped.startswith("#"):
            comment = stripped[1:].strip()
            if in_schedule:
                if current is not None:
                    current.comments.append(comment)
                else:
                    pending_comments.append(comment)
            continue
        code, inline = _split_comment(raw)
        code = code.rstrip()
        if not code.strip():
            if inline and in_schedule:
                pending_comments.append(inline)
            continue
        indent = _indent(code)
        key = code.strip()

        # Top-level key ends the on: block.
        if indent == 0 and not key.startswith("-"):
            if key.startswith("on:") or key == "on:":
                close()
                in_on = True
                on_indent = 0
                in_schedule = False
                schedule_indent = None
                pending_comments = []
                # Handle inline on: push or on: [push]
                rest = key[3:].strip()
                if rest and rest != "|":
                    # compact form, cannot contain schedule
                    in_on = False
                continue
            else:
                if in_on:
                    close()
                in_on = False
                in_schedule = False
                continue

        if not in_on:
            continue

        # Keys under on:
        if in_schedule and schedule_indent is not None and indent <= schedule_indent and not key.startswith("-"):
            close()
            in_schedule = False

        if (not in_schedule) and key.startswith("schedule:"):
            schedule_seen = True
            in_schedule = True
            schedule_indent = indent
            rest = key[len("schedule:") :].strip()
            if rest and rest not in {"|", ">", "[]"}:
                # unusual compact form
                pass
            continue

        if not in_schedule:
            continue

        # list item starting a schedule entry
        if key.startswith("- "):
            close()
            current = ScheduleEntry(path=path, line=idx, cron=None, timezone_name=None, comments=list(pending_comments))
            pending_comments = []
            item = key[2:].strip()
            if inline:
                current.comments.append(inline)
            if item.startswith("cron:"):
                current.cron = _strip_quotes(item[5:].strip())
            elif item.startswith("timezone:"):
                current.timezone_name = _strip_quotes(item[9:].strip())
            continue

        if current is None:
            # A key under schedule without a list item: treat as odd but keep going.
            continue

        if key.startswith("cron:"):
            current.cron = _strip_quotes(key[5:].strip())
            if inline:
                current.comments.append(inline)
        elif key.startswith("timezone:"):
            current.timezone_name = _strip_quotes(key[9:].strip())
            if inline:
                current.comments.append(inline)
        elif inline:
            current.comments.append(inline)

    close()
    if not schedule_seen:
        return None
    return entries


def extract_comment_intent(comments: list[str]) -> tuple[int, int, str] | None:
    """If comments mention a clock time AND a zone, return (hour, minute, zone).

    Comments that only say '9 点' without a zone are ignored, so this check
    does not false-fail on every annotation.
    """
    blob = " ".join(comments)
    if not blob.strip():
        return None
    zone = None
    for m in IANA_IN_TEXT.finditer(blob):
        token = m.group(0)
        if token in {"UTC", "GMT"}:
            zone = token
        else:
            try:
                load_zone(token)
                zone = token
            except ValueError:
                continue
    if zone is None:
        for city, zname in CITY_ALIASES.items():
            if city in blob:
                zone = zname
                break
    if zone is None:
        return None
    hm = CLOCK_HM.search(blob)
    if hm:
        return int(hm.group(1)), int(hm.group(2)), zone
    dian = CLOCK_DIAN.search(blob)
    if dian:
        hour = int(dian.group(1))
        if "下午" in (dian.group(0) or "") and hour < 12:
            hour += 12
        return hour, 0, zone
    return None


def next_wallclock_fires(hour: int, minute: int, tz: ZoneInfo, now: datetime, count: int = 1) -> list[datetime]:
    return next_cron_fires("%d %d * * *" % (minute, hour), tz, now, count)


def find_workflow_files(dir_path: Path) -> list[Path]:
    files = []
    for ext in ("*.yml", "*.yaml"):
        files.extend(sorted(dir_path.glob(ext)))
    return [p for p in files if p.is_file()]


def check_entry(entry: ScheduleEntry, now: datetime, count: int, show_zones: tuple[str, ...]) -> tuple[bool, list[str]]:
    lines: list[str] = []
    prefix = "%s:%d" % (entry.path, entry.line)
    if entry.parse_error:
        lines.append("REFUSE %s %s" % (prefix, entry.parse_error))
        return False, lines
    if not entry.cron:
        lines.append("REFUSE %s schedule entry has no cron:" % prefix)
        return False, lines
    try:
        Cron.parse(entry.cron)
    except ValueError as exc:
        lines.append("REFUSE %s invalid cron %r (%s)" % (prefix, entry.cron, exc))
        return False, lines

    missing_tz = False
    if not entry.timezone_name:
        tz = ZoneInfo("UTC")
        tz_label = "UTC (default)"
        missing_tz = True
        ok = False
    else:
        try:
            tz = load_zone(entry.timezone_name)
        except ValueError as exc:
            lines.append("REFUSE %s cron=%r timezone=%r (%s)" % (prefix, entry.cron, entry.timezone_name, exc))
            return False, lines
        tz_label = entry.timezone_name
        ok = True

    try:
        fires = next_cron_fires(entry.cron, tz, now, count)
    except Exception as exc:
        lines.append("REFUSE %s could not compute next fire times: %s" % (prefix, exc))
        return False, lines

    intent = extract_comment_intent(entry.comments)
    comment_error = None
    comment_ok = None
    if intent:
        hour, minute, zone_name = intent
        intent_tz = load_zone(zone_name)
        intended = next_wallclock_fires(hour, minute, intent_tz, now, 1)[0]
        actual = fires[0]
        if intended != actual:
            comment_error = (
                "nearby comment time %02d:%02d %s next-fires at %s, but the schedule next-fires at %s"
                % (hour, minute, zone_name, format_instant(intended, show_zones), format_instant(actual, show_zones))
            )
            ok = False
        else:
            comment_ok = "comment check: %02d:%02d %s matches the next fire" % (hour, minute, zone_name)

    if missing_tz:
        lines.append(
            "REFUSE %s cron=%r timezone=<missing, defaults to UTC>"
            % (prefix, entry.cron)
        )
        lines.append("  reason: every on.schedule entry must declare a valid IANA timezone:")
    elif comment_error:
        lines.append("REFUSE %s cron=%r timezone=%s" % (prefix, entry.cron, entry.timezone_name))
        lines.append("  reason: %s" % comment_error)
    else:
        lines.append("ALLOW %s cron=%r timezone=%s" % (prefix, entry.cron, entry.timezone_name))

    lines.append("  next %d fire times (computed with zoneinfo from --now; not a live Actions run) in %s:" % (count, tz_label))
    for i, fire in enumerate(fires, 1):
        lines.append("    %d. %s" % (i, format_instant(fire, show_zones)))
    if comment_ok:
        lines.append("  %s" % comment_ok)
    return ok, lines


def run_gate(paths: list[Path], now: datetime, count: int, show_zones: tuple[str, ...]) -> int:
    print("cron-timezone-gate: now=%s" % format_instant(now, show_zones))
    print("cron-timezone-gate: scanned %d file(s)" % len(paths))
    any_schedule = False
    all_ok = True
    if not paths:
        print("cron-timezone-gate: no workflow files found; OK (nothing to check)")
        return 0
    for path in paths:
        print("--- %s" % path)
        try:
            entries = parse_schedule_entries(path)
        except Exception as exc:
            print("REFUSE %s failed to parse: %s" % (path, exc))
            all_ok = False
            continue
        if entries is None:
            print("  no on.schedule key; ignored")
            continue
        any_schedule = True
        if not entries:
            print("REFUSE %s has on.schedule but no cron entries" % path)
            all_ok = False
            continue
        for entry in entries:
            ok, lines = check_entry(entry, now, count, show_zones)
            for line in lines:
                print(line)
            if not ok:
                all_ok = False
    if not any_schedule:
        print("cron-timezone-gate: no scheduled workflows; OK")
        return 0
    if all_ok:
        print("cron-timezone-gate: ALLOW")
        return 0
    print("cron-timezone-gate: REFUSE")
    return 1


def parse_now(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("--now must include a timezone offset, got %r" % value)
    return dt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Require IANA timezone: on every GitHub Actions schedule entry")
    p.add_argument("--workflows-dir", default="")
    p.add_argument("--path", action="append", default=[])
    p.add_argument("--now", default="", help="ISO-8601 instant with offset, used only for next-run computation")
    p.add_argument("--next", type=int, default=3)
    p.add_argument("--show-zones", default="UTC,Asia/Shanghai,America/Fortaleza")
    args = p.parse_args(argv)
    if args.next < 1:
        print("cron-timezone-gate: --next must be >= 1", file=sys.stderr)
        return 2
    show_zones = tuple(z.strip() for z in args.show_zones.split(",") if z.strip())
    for z in show_zones:
        if z not in {"UTC", "GMT"}:
            load_zone(z)
    if args.now:
        now = parse_now(args.now)
    else:
        now = datetime.now(tz=UTC)
    paths: list[Path] = []
    if args.path:
        paths.extend(Path(p) for p in args.path)
    else:
        root = Path(args.workflows_dir) if args.workflows_dir else Path(".github/workflows")
        if not root.exists():
            print("cron-timezone-gate: %s does not exist; OK (nothing to check)" % root)
            return 0
        paths = find_workflow_files(root)
    return run_gate(paths, now, args.next, show_zones)


if __name__ == "__main__":
    raise SystemExit(main())
