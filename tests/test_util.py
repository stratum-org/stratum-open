from stratum_open.util import count


def test_count_is_singular_only_for_one():
    assert count(1, "file") == "1 file"
    assert [count(n, "serious finding") for n in (0, 2)] == ["0 serious findings", "2 serious findings"]


def test_retry_after_seconds_and_invalid_values():
    from stratum_open.util import retry_after

    assert retry_after(" 1.5 ", 9) == 1.5
    assert retry_after("-2", 9) == 0
    assert retry_after(None, 9) == 9
    assert retry_after("tomorrow", 9) == 9


def test_retry_after_dates_with_and_without_time_zone(monkeypatch):
    from datetime import datetime, timezone

    from stratum_open import util

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(util, "datetime", FixedClock)
    assert util.retry_after("Thu, 12 Nov 2026 12:00:08 GMT", 9) == 8
    assert util.retry_after("Thu, 12 Nov 2026 12:00:08", 9) == 8
    assert util.retry_after("Thu, 12 Nov 2026 11:59:00 GMT", 9) == 0
