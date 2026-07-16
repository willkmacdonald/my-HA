"""timeparse unit tests — table-driven over supported forms + rejection cases (spec §Testing)."""

import pytest
import timeparse

# --- durations ---


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("ten minutes", 600),
        ("10 minutes", 600),
        ("1 minute", 60),
        ("a minute", 60),
        ("an hour", 3600),
        ("two hours", 7200),
        ("twenty minutes", 1200),
        ("twenty five minutes", 1500),
        ("twenty-five minutes", 1500),
        ("45 seconds", 45),
        ("an hour and a half", 5400),
        ("one and a half hours", 5400),
        ("half an hour", 1800),
        ("1 hour and 20 minutes", 4800),
    ],
)
def test_parse_duration_accepts(text: str, seconds: int) -> None:
    assert timeparse.parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing
        "eleventy minutes",  # not a number word
        "0 minutes",  # zero rejected
        "25 hours",  # > 24 h sanity cap
        "ten",  # number without unit
        "minutes",  # unit without number
    ],
)
def test_parse_duration_rejects(text: str) -> None:
    assert timeparse.parse_duration(text) is None
