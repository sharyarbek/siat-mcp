from datetime import date

from siat.periods import parse_period


def test_year():
    p = parse_period("2025")
    assert (p.type, p.start, p.end) == ("Y", date(2025, 1, 1), date(2025, 12, 31))


def test_quarter():
    p = parse_period("2025-Q3")
    assert (p.type, p.start, p.end) == ("Q", date(2025, 7, 1), date(2025, 9, 30))
    assert parse_period("2024-Q1").end == date(2024, 3, 31)


def test_month_and_leap_year():
    assert parse_period("2024-M02").end == date(2024, 2, 29)
    assert parse_period("2025-M02").end == date(2025, 2, 28)
    assert parse_period("2025-M12").type == "M"


def test_unknown_kept_raw():
    p = parse_period("2025-H1")
    assert p.type == "X" and p.raw == "2025-H1" and p.start is None
    assert parse_period("2025-M13").type == "X"
    assert parse_period("2025-Q5").type == "X"
