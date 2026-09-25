"""Run service code "at" a chosen time: replaces `now` in every app.services module.

Services import `now` into their own namespace (from app.db import now), so that name is
what gets swapped. Used by the tests' `clock` fixture and by `app.seed --history`, so
generated history goes through exactly the same code paths as live service.
"""
import importlib
import pkgutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime


def _service_modules():
    import app.services as pkg

    yield pkg
    for info in pkgutil.iter_modules(pkg.__path__):
        yield importlib.import_module(f"app.services.{info.name}")


@contextmanager
def override_now(clock: Callable[[], datetime]) -> Iterator[Callable[[], datetime]]:
    """Within the block, every service's now() returns clock()."""
    saved = [(m, m.now) for m in _service_modules() if hasattr(m, "now")]
    for module, _ in saved:
        module.now = clock
    try:
        yield clock
    finally:
        for module, original in saved:
            module.now = original
