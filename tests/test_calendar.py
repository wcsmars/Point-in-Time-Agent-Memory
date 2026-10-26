import pytest
import importlib.util

from pit_memory.calendar import next_trading_session

pytestmark = pytest.mark.skipif(importlib.util.find_spec("exchange_calendars") is None, reason="exchange_calendars dependency unavailable; install the package to test real XNYS sessions")


@pytest.mark.parametrize("filed,expected", [
    ("2024-03-28", "2024-04-01"),  # Good Friday closure
    ("2024-05-24", "2024-05-28"),  # Memorial Day
    ("2024-07-04", "2024-07-05"),
    ("2024-07-05", "2024-07-08"),
    ("2024-06-18", "2024-06-20"),  # Juneteenth
    ("2012-10-26", "2012-10-31"),  # Hurricane Sandy unscheduled closures
    ("2001-09-10", "2001-09-17"),  # closed after the September 11 attacks
    ("2018-12-04", "2018-12-06"),  # national day of mourning, President G. H. W. Bush
    ("2025-01-08", "2025-01-10"),  # national day of mourning, President Carter
    ("2024-12-31", "2025-01-02"),  # next session falls in the following year
    ("2026-12-31", "2027-01-04"),  # New Year's Day on Friday, then the weekend
    ("2000-01-01", "2000-01-03"),  # first supported year
    ("2100-12-31", "2101-01-03"),  # last supported year; answer in 2101
])
def test_next_real_xnys_session(filed, expected):
    assert next_trading_session(filed) == expected


@pytest.mark.parametrize("filed", ["1900-01-01", "1999-12-31", "2101-01-01"])
def test_calendar_rejects_uncovered_era(filed):
    with pytest.raises(ValueError, match="supported"):
        next_trading_session(filed)


def test_dates_out_of_year_order_share_one_calendar_build():
    from pit_memory import calendar
    calendar._calendar.cache_clear()
    calendar.next_trading_session.cache_clear()
    for filed in ("2026-02-02", "2009-02-02", "2018-02-02", "2011-02-02", "2024-02-02", "2013-02-02"):
        next_trading_session(filed)
    assert calendar._calendar.cache_info().misses == 1
