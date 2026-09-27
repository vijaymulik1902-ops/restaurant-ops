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


def whole_rupees(paise: int) -> str:
    """Paise as whole rupees with Indian grouping, for sentences: 93910000 -> '₹9,39,100'."""
    sign = "-" if paise < 0 else ""
    digits = str(round(abs(paise) / 100))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}"


def initials(name: str) -> str:
    """Badge initials: first letters of the first two words, or the first two letters of a
    single name ("Rahul" -> "RA", "Rahul Shah" -> "RS")."""
    words = [w for w in (name or "").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def role_label(role: str, station: str | None = None, section: str | None = None) -> str:
    """'Manager', 'Counter', 'Chef · tandoor', 'Waiter · A'."""
    base = role.capitalize()
    extra = station if role == "chef" else section if role == "waiter" else None
    return f"{base} · {extra}" if extra else base
