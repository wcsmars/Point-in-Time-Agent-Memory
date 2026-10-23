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
])
def test_next_real_xnys_session(filed, expected):
    assert next_trading_session(filed) == expected


def test_calendar_rejects_uncovered_era():
    with pytest.raises(ValueError, match="supported"):
        next_trading_session("1900-01-01")
