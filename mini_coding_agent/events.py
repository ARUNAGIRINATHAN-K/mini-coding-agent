"""Versioned, in-process lifecycle events for all user interfaces."""

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .context import now

EVENT_SCHEMA_VERSION = "1.0"
EventCallback = Callable[["AgentEvent"], None]


@dataclass(frozen=True)
class AgentEvent:
    version: str
    event_id: str
    run_id: str | None
    session_id: str
    sequence: int
    type: str
    timestamp: str
    data: dict

    def to_dict(self):
        return asdict(self)


class EventEmitter:
    """Assigns ordered event envelopes and forwards them to subscribers."""

    def __init__(self, callback: EventCallback | None = None):
        self.callbacks = [callback] if callback else []
        self.sequence = 0

    def subscribe(self, callback: EventCallback):
        self.callbacks.append(callback)

    def emit(self, event_type, *, session_id, run_id=None, **data):
        self.sequence += 1
        event = AgentEvent(
            version=EVENT_SCHEMA_VERSION, event_id=uuid.uuid4().hex, run_id=run_id,
            session_id=session_id, sequence=self.sequence, type=event_type,
            timestamp=now(), data=data,
        )
        for callback in tuple(self.callbacks):
            callback(event)
        return event
