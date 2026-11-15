"""Collection window and timestamp parsing.

Day boundaries follow the configured timezone; commits are attributed by committer date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import messages as m


@dataclass(frozen=True)
class Period:
    """The half-open collection window [start, end) plus the timezone needed to read it.

    Every source shares this one definition of "this period".
    """

    start: datetime
    end: datetime
    tz: timezone

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def last_day(self) -> datetime:
        return self.end - timedelta(seconds=1)

    @property
    def single_day(self) -> bool:
        return self.start.date() == self.last_day.date()

    def contains(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment < self.end


def timezone_from_offset(offset: float) -> timezone:
    return timezone(timedelta(hours=offset))


def parse_ts(s: str, tz: timezone) -> datetime | None:
    if not s:
        return None
    try:
        value = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz)
    except ValueError:
        return None


def epoch_ms(ms, tz: timezone) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _day_start(day, tz: timezone) -> datetime:
    """Midnight of `day`, rejecting arithmetic that leaves the representable range.

    `--days 3000000` or `--date 9999-12-31` would otherwise raise OverflowError deep in the
    date math, which reaches the user as a traceback instead of a usable message.
    """
    try:
        return datetime.combine(day, datetime.min.time(), tz)
    except (OverflowError, ValueError) as exc:
        raise ValueError(m.TW_ERR_DAY_OVERFLOW.format(day=day)) from exc


def _shift(day, days: int):
    try:
        return day + timedelta(days=days)
    except (OverflowError, ValueError) as exc:
        raise ValueError(m.TW_ERR_SHIFT_OVERFLOW.format(day=day, days=days)) from exc


def resolve_period(date_arg: str | None, days: int, tz: timezone, week: str | None = None,
                   today=None) -> Period:
    """Return a half-open date interval [start, end) in the configured timezone.

    week: "this" = Monday of this week through today, "last" = Monday through Sunday of
    last week; date_arg and days are ignored in that case. `today` is a test seam.
    """
    today = today or datetime.now(tz).date()
    if week:
        monday = _shift(today, -today.weekday())
        if week == "last":
            start_day, end_day = _shift(monday, -7), _shift(monday, -1)
        else:
            start_day, end_day = monday, today
        return Period(_day_start(start_day, tz), _day_start(_shift(end_day, 1), tz), tz)
    if date_arg in ("today", "yesterday"):
        end_day = today - timedelta(days=1 if date_arg == "yesterday" else 0)
    elif date_arg:
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != date_arg:
                raise ValueError
            end_day = parsed.date()
        except ValueError as e:
            raise ValueError(m.TW_ERR_DATE_FORMAT.format(value=date_arg)) from e
    else:
        end_day = today
    end = _day_start(_shift(end_day, 1), tz)
    try:
        start = end - timedelta(days=days)
    except (OverflowError, ValueError) as exc:
        raise ValueError(m.TW_ERR_DAYS_OVERFLOW.format(days=days)) from exc
    return Period(start, end, tz)
