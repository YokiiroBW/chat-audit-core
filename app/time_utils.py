import datetime as dt


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)

