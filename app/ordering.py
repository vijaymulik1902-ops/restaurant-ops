"""Seniority order for anywhere staff are listed (login, Staff page, audit filters):
manager, counter, chefs by station (tandoor, kitchen, bar), waiters by section (A to G), then name."""

ROLE_ORDER = {"manager": 0, "counter": 1, "chef": 2, "waiter": 3}
STATION_ORDER = {"tandoor": 0, "kitchen": 1, "bar": 2}


def seniority_key(role: str, station: str | None, section: str | None, name: str) -> tuple:
    return (
        ROLE_ORDER.get(role, 9),
        STATION_ORDER.get(station or "", 9) if role == "chef" else 0,
        (section or "~") if role == "waiter" else "",
        name.lower(),
    )
