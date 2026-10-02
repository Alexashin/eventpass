from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    """UTC timestamp stored as a naive value for the existing DB schema."""
    return datetime.now(UTC).replace(tzinfo=None)
