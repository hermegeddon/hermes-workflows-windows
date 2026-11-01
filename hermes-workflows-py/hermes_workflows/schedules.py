"""Small five-field cron evaluator. UTC works without a timezone database."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Cron:
    def __init__(self, expression: str, zone: str = "UTC"):
        interval = (
            re.fullmatch(r"every\s+([1-9][0-9]*)\s*([smhd])", expression, re.I)
            if isinstance(expression, str)
            else None
        )
        if not interval and (not isinstance(expression, str) or len(expression.split()) != 5):
            raise ValueError("cron must contain five fields (minute hour day month weekday)")
        try:
            self.zone = timezone.utc if zone in ("UTC", "Etc/UTC") else ZoneInfo(zone)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError(
                f"timezone '{zone}' is unavailable; install tzdata in Hermes or use UTC"
            ) from exc
        self.interval = None
        if interval:
            seconds = (
                int(interval[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[interval[2].lower()]
            )
            # Hermes' interval parser rounds sub-minute schedules up.
            self.interval = timedelta(minutes=max(1, (seconds + 59) // 60))
            return
        self.parts = expression.split()
        self.values = [
            self._field(p, lo, hi)
            for p, lo, hi in zip(self.parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7), strict=True)
        ]
        if 7 in self.values[4]:
            self.values[4].add(0)

    @staticmethod
    def _field(text: str, lo: int, hi: int) -> set[int]:
        result = set()
        try:
            for piece in text.split(","):
                base, slash, step = piece.partition("/")
                step = int(step) if slash else 1
                if step < 1:
                    raise ValueError
                if base == "*":
                    start, end = lo, hi
                elif "-" in base:
                    start, end = map(int, base.split("-"))
                else:
                    start = int(base)
                    end = hi if slash else start
                if not lo <= start <= end <= hi:
                    raise ValueError
                result.update(range(start, end + 1, step))
        except ValueError as exc:
            raise ValueError(f"invalid cron field '{text}' (range {lo}..{hi})") from exc
        return result

    def matches(self, when: datetime) -> bool:
        if self.interval is not None:
            raise ValueError("intervals are relative cadences, not wall-clock matches")
        local = when.astimezone(self.zone)
        minute, hour, day, month, weekday = self.values
        dom, dow = local.day in day, (local.weekday() + 1) % 7 in weekday
        day_match = dom or dow if self.parts[2] != "*" and self.parts[4] != "*" else dom and dow
        return local.minute in minute and local.hour in hour and local.month in month and day_match

    def next(self, after: datetime | None = None) -> datetime:
        if self.interval is not None:
            return (after or datetime.now(timezone.utc)).astimezone(timezone.utc) + self.interval
        current = (after or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
            second=0, microsecond=0
        ) + timedelta(minutes=1)
        limit = current + timedelta(days=366 * 8)
        while current < limit:
            local = current.astimezone(self.zone)
            # Jump whole hours on days that cannot match; preserves UTC iteration
            # across DST transitions and leap years.
            if local.month not in self.values[3]:
                jump = timedelta(hours=1)
                # Do not jump over the first eligible minute of a new month.
                # The local boundary may also fall between UTC hour boundaries.
                if (current + jump).astimezone(self.zone).month != local.month:
                    jump = timedelta(minutes=1)
                current += jump
                continue
            if self.matches(current):
                return current
            current += timedelta(minutes=1)
        raise ValueError("cron has no occurrence in the next eight years")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
