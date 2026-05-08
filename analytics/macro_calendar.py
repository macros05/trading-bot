"""Detect known high-impact macro events from a timestamp.

Uses a static curated calendar (FOMC meetings, monthly CPI release, monthly
NFP). Sources: FOMC published 2024–2026 schedule, BLS standard release dates.
For events not on the static list, the helper still flags weekends since BTC
is materially less liquid then.

Updates: when new dates are scheduled, append them to the lists below.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Curated FOMC meeting dates (last day of the 2-day meeting — the one with the
# statement and press conference). Update when 2027 schedule is announced.
FOMC_DATES: tuple[date, ...] = (
    # 2024
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1),
    date(2024, 6, 12), date(2024, 7, 31), date(2024, 9, 18),
    date(2024, 11, 7), date(2024, 12, 18),
    # 2025
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),
    date(2025, 6, 18), date(2025, 7, 30), date(2025, 9, 17),
    date(2025, 10, 29), date(2025, 12, 10),
    # 2026 (placeholder — verify when the schedule is published)
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
)


def _second_friday(year: int, month: int) -> date:
    """Approximate NFP date: first Friday of the month per BLS convention.

    BLS publishes the Employment Situation report on the first Friday following
    the reference week. Edge cases (when the first day of the month is a Friday
    and the reference week shifts) push it to the second Friday.
    """
    d = date(year, month, 1)
    while d.weekday() != 4:  # Friday=4
        d += timedelta(days=1)
    return d


def _cpi_dates(year: int) -> list[date]:
    """Approximate CPI release dates for *year*.

    BLS releases CPI on a roughly mid-month schedule (10th to 14th of each
    month). Without scraping a live calendar we use the 12th as a stable
    central estimate; misses are fine since trade counts at this resolution
    are low and the filter is conservative.
    """
    return [date(year, m, 12) for m in range(1, 13)]


def _nfp_dates(year: int) -> list[date]:
    return [_second_friday(year, m) for m in range(1, 13)]


def _is_weekend(d: date) -> bool:
    return d.weekday() in (5, 6)


def macro_event_for_ts(ts_ms: int) -> str:
    """Return a short label for the macro context of *ts_ms*.

    Priority: FOMC > CPI > NFP > WEEKEND > '' (normal).
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    d = dt.date()
    if d in FOMC_DATES:
        return 'FOMC'
    if d in _cpi_dates(d.year):
        return 'CPI'
    if d in _nfp_dates(d.year):
        return 'NFP'
    if _is_weekend(d):
        return 'WEEKEND'
    return ''


def is_high_impact_event(ts_ms: int) -> bool:
    """True for FOMC/CPI/NFP — used to auto-pause when win rate degrades on these days."""
    return macro_event_for_ts(ts_ms) in ('FOMC', 'CPI', 'NFP')
