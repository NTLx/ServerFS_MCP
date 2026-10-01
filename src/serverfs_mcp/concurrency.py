"""Process-global concurrency primitives shared by every filesystem kernel.

This module is platform-neutral by design: the product layer imports
``mutation_lock`` here instead of through ``mutations.py`` (which carries the
Linux fdio kernel), so a non-Linux process can acquire the same serialization
without ever importing FD primitives (§11 import safety).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator

_MUTATION_LOCK = threading.RLock()


@contextlib.contextmanager
def mutation_lock() -> Iterator[None]:
    """Serialize every mutation in this process.

    Reads deliberately do not take this lock. One re-entrant lock, not a
    per-path table: the MCP tool layer takes it once before the shared Agent
    lease, while the mutation kernel re-enters it internally. Cross-thread
    serialization remains unchanged.
    """
    with _MUTATION_LOCK:
        yield


__all__ = ["mutation_lock"]
