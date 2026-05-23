"""In-memory pub/sub for case + submission lifecycle events.

Lets the FastAPI app stream Server-Sent Events to a browser without each
client polling the SQLite DB on a fixed interval. The orchestrator (and
the doctor-submit pipeline) publishes events; an SSE endpoint subscribes
and forwards them downstream.

Scope is intentionally narrow:
  - One process, one event loop. The dict lives in module state.
  - Subscribers get only events that fire AFTER they subscribe; the SSE
    endpoint is responsible for emitting the current snapshot first.
  - When a topic publishes a `terminal=True` event, every queued subscriber
    receives it and the dispatcher hangs up.

If we ever move to multi-worker, swap this for Redis pub/sub or a fanout
channel — the publish/subscribe API stays the same.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


# topic = a stable string like "case:<case_id>" or "submission:<sub_id>"
_subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)


def subscribe(topic: str) -> asyncio.Queue[dict[str, Any]]:
    """Return a fresh queue that will receive every future event on `topic`.
    Caller must `unsubscribe(topic, queue)` when done."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _subscribers[topic].append(q)
    return q


def unsubscribe(topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
    queue_list = _subscribers.get(topic)
    if not queue_list:
        return
    try:
        queue_list.remove(queue)
    except ValueError:
        pass
    if not queue_list:
        _subscribers.pop(topic, None)


async def publish(topic: str, event: dict[str, Any]) -> None:
    """Fan out an event to every current subscriber of `topic`. Non-blocking:
    a slow subscriber can't stall the producer."""
    for q in list(_subscribers.get(topic, ())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # defensive — we don't set a maxsize
            pass


def subscriber_count(topic: str) -> int:
    return len(_subscribers.get(topic, ()))
