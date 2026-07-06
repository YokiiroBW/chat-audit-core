import datetime as dt

from app.time_utils import format_utc_z, parse_utc_datetime, timestamp_to_utc, to_utc_naive, utc_to_timestamp


def test_timestamp_conversion_uses_utc():
    value = timestamp_to_utc(0)

    assert value == dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    assert utc_to_timestamp(value) == 0


def test_format_utc_z_normalizes_aware_datetime():
    value = dt.datetime(2026, 7, 6, 10, 30, 15, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    assert format_utc_z(value) == "2026-07-06T02:30:15Z"


def test_parse_utc_datetime_accepts_z_and_offsets_as_naive_utc():
    assert parse_utc_datetime("2026-07-06T02:30:15Z") == dt.datetime(2026, 7, 6, 2, 30, 15)
    assert parse_utc_datetime("2026-07-06T10:30:15+08:00") == dt.datetime(2026, 7, 6, 2, 30, 15)


def test_to_utc_naive_keeps_naive_values_unchanged():
    value = dt.datetime(2026, 7, 6, 2, 30, 15)

    assert to_utc_naive(value) == value
