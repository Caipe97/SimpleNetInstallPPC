"""Logging setup and an in-process event bus.

Every subsystem logs through a ``netboot.<subsystem>`` logger. Structured
events (a client asked for the image list, a TFTP transfer finished, ...)
also go to the :class:`EventBus`, which keeps a ring buffer. The web UI
reads the buffer and subscribes to live events from it.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional


@dataclass
class Event:
    subsystem: str          # "dhcp", "bsdp", "tftp", "http", ...
    kind: str               # short machine-readable name, e.g. "select"
    message: str            # human-readable line
    client: Optional[str] = None   # client MAC (aa:bb:cc:dd:ee:ff) or IP
    data: Dict[str, object] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    level: int = logging.INFO
    seq: int = 0            # increasing number, so a reader can ask for what it missed


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, capacity: int = 5000) -> None:
        self._buffer: Deque[Event] = collections.deque(maxlen=capacity)
        self._subscribers: List[Subscriber] = []
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, subsystem: str, kind: str, message: str, *,
             client: Optional[str] = None, level: int = logging.INFO,
             **data: object) -> Event:
        event = Event(subsystem, kind, message, client, dict(data), level=level)
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            self._buffer.append(event)
            subscribers = list(self._subscribers)
        prefix = f"[{client}] " if client else ""
        logging.getLogger(f"netboot.{subsystem}").log(level, "%s%s", prefix, message)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # a broken subscriber must not break a service
                logging.getLogger("netboot.events").exception("event subscriber failed")
        return event

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)
        return unsubscribe

    def recent(self, limit: int = 200) -> List[Event]:
        with self._lock:
            return list(self._buffer)[-limit:]

    def since(self, seq: int, limit: int = 1000) -> List[Event]:
        """Events newer than ``seq`` still in the buffer (the last ``limit`` of them)."""
        with self._lock:
            return [e for e in self._buffer if e.seq > seq][-limit:]


def setup_logging(verbose: bool = False, log_file: Optional[Path] = None) -> None:
    root = logging.getLogger("netboot")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.handlers[:] = [console]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
