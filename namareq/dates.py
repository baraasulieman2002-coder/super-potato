"""Date arithmetic for recurring schedules."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

FREQUENCIES = ("one_time", "weekly", "monthly", "quarterly", "semi_annually", "annually", "custom")
MONTHS_PER = {"monthly": 1, "quarterly": 3, "semi_annually": 6, "annually": 12}
PERIODS_PER_YEAR = {"weekly": 52, "monthly": 12, "quarterly": 4, "semi_annually": 2, "annually": 1}


def d(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def add_months(start: date, months: int, anchor_day: int | None = None) -> date:
    """Add months, clamping to month end (Jan 31 + 1 month = Feb 28/29).
    ``anchor_day`` keeps the original day when later months are long enough."""
    day = anchor_day or start.day
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def advance(start, frequency: str, n: int = 1, custom_days: int | None = None,
            anchor_day: int | None = None) -> date:
    start = d(start)
    if frequency == "weekly":
        return start + timedelta(weeks=n)
    if frequency in MONTHS_PER:
        return add_months(start, MONTHS_PER[frequency] * n, anchor_day)
    if frequency == "custom":
        if not custom_days or custom_days <= 0:
            raise ValueError("custom frequency needs a positive day interval")
        return start + timedelta(days=custom_days * n)
    raise ValueError(f"no next occurrence for frequency {frequency}")


def occurrences(first, frequency: str, until, custom_days: int | None = None,
                end=None) -> list[date]:
    """All occurrence dates from ``first`` up to and including ``until``."""
    first, until = d(first), d(until)
    if end:
        until = min(until, d(end))
    if frequency == "one_time":
        return [first] if first <= until else []
    out, n = [], 0
    current = first
    while current <= until:
        out.append(current)
        n += 1
        current = advance(first, frequency, n, custom_days, anchor_day=first.day)
    return out


def month_bounds(on) -> tuple[date, date]:
    on = d(on)
    return date(on.year, on.month, 1), date(on.year, on.month, calendar.monthrange(on.year, on.month)[1])


def months_between(a, b) -> float:
    """Approximate months between two dates (average month = 30.4375 days)."""
    return (d(b) - d(a)).days / 30.4375
