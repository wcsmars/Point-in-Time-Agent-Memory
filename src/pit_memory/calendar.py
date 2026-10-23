"""Conservative date-only SEC availability using real XNYS trading sessions."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from datetime import date, timedelta
import warnings

from .models import iso_date


@lru_cache(maxsize=8)
def _calendar(year: int):
    import exchange_calendars as xcals

    # Explicit range avoids dependency defaults tied to today's date.
    with _quiet_dependency():
        return xcals.get_calendar("XNYS", start=f"{year-1}-01-01", end=f"{year+2}-12-31")


@contextmanager
def _quiet_dependency():
    """Silence a pandas deprecation raised inside exchange_calendars, not here."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Series.values returning an ndarray")
        yield


@lru_cache(maxsize=4096)
def next_trading_session(filed: str) -> str:
    """First XNYS session strictly AFTER the filing date (not same-day).

    The SEC date field lacks an acceptance timestamp. Waiting until the next
    trading session is conservative and handles weekends, holidays, closures.
    Historical support is deliberately bounded to the SEC XBRL era.
    """
    iso_date(filed)
    day = date.fromisoformat(filed)
    if not 2000 <= day.year <= 2100:
        raise ValueError("Filing date outside supported calendar years 2000-2100")
    cal = _calendar(day.year)
    with _quiet_dependency():
        result = cal.date_to_session(day + timedelta(days=1), direction="next")
    return result.date().isoformat()
