import datetime as dt


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def utc_now_aware() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def to_utc_naive(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.UTC).replace(tzinfo=None)


def timestamp_to_utc(timestamp: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)


def utc_to_timestamp(value: dt.datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return int(value.astimezone(dt.UTC).timestamp())


def format_utc_z(value: dt.datetime) -> str:
    normalized = to_utc_naive(value).replace(microsecond=0)
    return normalized.isoformat(timespec="seconds") + "Z"


def parse_utc_datetime(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    return to_utc_naive(parsed)
