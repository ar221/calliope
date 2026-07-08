"""SSE event bus.

Bounded per-subscriber queues shared between the HTTP handler (subscribe /
unsubscribe on /events) and the formatter pipeline (broadcast of
dictation-token / dictation-state / dictation-command events). Module-level
state lives here so both the executable wrapper and calliope_server.formatter
operate on the SAME subscriber list. Extracted from the executable
`calliope-server` script (Stage 4 split).
"""

import logging
import queue
import threading

log = logging.getLogger("dictation-server")

# ─── Phase 2 — SSE event bus ──────────────────────────────
# Each subscriber gets its own bounded queue. Full queues drop events rather
# than block broadcasters — SSE is best-effort. Typical subscriber: one ST tab.
EVENT_QUEUE_MAX = 50
event_subscribers: list["queue.Queue"] = []
event_subscribers_lock = threading.Lock()


def subscribe_events() -> "queue.Queue":
    q: "queue.Queue" = queue.Queue(maxsize=EVENT_QUEUE_MAX)
    with event_subscribers_lock:
        event_subscribers.append(q)
    return q


def unsubscribe_events(q: "queue.Queue") -> None:
    with event_subscribers_lock:
        try:
            event_subscribers.remove(q)
        except ValueError:
            pass


def broadcast_event(event_type: str, payload: dict) -> int:
    """Push an event to every live subscriber. Returns # subscribers reached.

    Drops on a per-subscriber basis when that subscriber's queue is full;
    a slow consumer can't stall others.
    """
    msg = (event_type, payload)
    reached = 0
    with event_subscribers_lock:
        subs = list(event_subscribers)
    for q in subs:
        try:
            q.put_nowait(msg)
            reached += 1
        except queue.Full:
            log.warning("SSE subscriber queue full; dropping event %s", event_type)
    return reached
