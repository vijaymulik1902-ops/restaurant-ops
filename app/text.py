"""Small wording helpers shared by templates (as Jinja filters) and service messages."""
from datetime import date


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    """'1 bill', '2 bills', '0 bills'; irregular words pass their plural: plural(3, 'entry', 'entries')."""
    word = singular if n == 1 else (plural_form or singular + "s")
    return f"{n} {word}"


def date_range(start: date, end: date) -> str:
    """Compact human range: '25 Sep 2026', '1–25 Sep 2026', '28 Aug – 3 Sep 2026',
    '28 Dec 2025 – 3 Jan 2026'."""
    if start == end:
        return f"{start.day} {start:%b %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {end:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"
