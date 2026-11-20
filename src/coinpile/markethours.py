"""US stock market days (NYSE and Nasdaq share them), worked out from the exchanges' rules, so there's no list to keep up to date.

Closed: weekends and
  New Year's Day, Martin Luther King Jr. Day (3rd Monday of January), Washington's Birthday (3rd Monday of February),
  Good Friday, Memorial Day (last Monday of May), Juneteenth (June 19, since 2022), Independence Day (July 4),
  Labor Day (1st Monday of September), Thanksgiving (4th Thursday of November), Christmas Day.
A holiday on a Saturday is observed the Friday before and one on a Sunday the Monday after, except New Year's Day on a Saturday,
which isn't made up (the Friday before is the last trading day of the year).

Early close (1:00 pm New York): the day before Independence Day, the day after Thanksgiving and Christmas Eve, when they are trading days.

One-off closures (a national day of mourning, a storm) can't be worked out: add them to `extra_closed` (YYYY-MM-DD)."""
from datetime import date, time, timedelta

OPEN = time(9, 30)
EARLY_CLOSE = time(13, 0)
CLOSE = time(16, 0)


def _easter(y: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(y, month, (h + l - 7 * m + 114) % 31 + 1)


def _nth(y: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) weekday (Mon=0) of a month; n=-1 is the last."""
    if n > 0:
        d = date(y, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    d = date(y + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    if d.weekday() == 5:
        return None if (d.month, d.day) == (1, 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def holidays(y: int) -> dict[date, str]:
    out = {}
    fixed = [(date(y, 1, 1), "New Year's Day"), (date(y, 7, 4), "Independence Day"), (date(y, 12, 25), "Christmas Day")]
    if y >= 2022:
        fixed.append((date(y, 6, 19), "Juneteenth"))
    for d, name in fixed:
        o = _observed(d)
        if o:
            out[o] = name
    out[_nth(y, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth(y, 2, 0, 3)] = "Washington's Birthday"
    out[_easter(y) - timedelta(days=2)] = "Good Friday"
    out[_nth(y, 5, 0, -1)] = "Memorial Day"
    out[_nth(y, 9, 0, 1)] = "Labor Day"
    out[_nth(y, 11, 3, 4)] = "Thanksgiving Day"
    return out


def closed(d: date, extra: list[str] | tuple = ()) -> str | None:
    """Why the market is closed on d (None when it's open)."""
    if d.weekday() >= 5:
        return "the weekend"
    if d.isoformat() in set(extra or ()):
        return "a closure you added"
    return holidays(d.year).get(d)


def close_time(d: date, extra: list[str] | tuple = ()) -> time | None:
    """When the market closes on d: 4 pm, 1 pm on an early-close day, None when closed."""
    if closed(d, extra):
        return None
    july4 = date(d.year, 7, 4)
    early = {july4 - timedelta(days=1), _nth(d.year, 11, 3, 4) + timedelta(days=1), date(d.year, 12, 24)}
    return EARLY_CLOSE if d in early else CLOSE
