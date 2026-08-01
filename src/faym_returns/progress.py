"""Cooperative abort for a long-running session.

A run is mostly spent inside the deliberate pauses between line items, so a
Ctrl-C landing at an arbitrary instruction could interrupt the agent midway
through submitting a return - leaving a request half-filed with nothing recorded
against it.

Instead the interrupt sets a flag, and the agent checks that flag wherever it
sleeps. The run then unwinds at the next pause, which is by construction never
mid-submission, and the outcomes gathered so far are still written back.

The signal is a module-level singleton rather than a parameter threaded through
every call: the agent already forbids two concurrent sessions against one
account, so "one active run per process" is an existing invariant.
"""

from __future__ import annotations

import threading


class StopSignal:
    """A request to wind the current run down at the next safe point."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    @property
    def requested(self) -> bool:
        return self._event.is_set()


stop = StopSignal()


def reset() -> None:
    """Clear any pending stop before starting a fresh run."""
    stop.clear()


def check_stop(message: str = "Stopped by the operator.") -> None:
    """Raise if a stop was requested. Imported lazily to avoid a cycle."""
    if stop.requested:
        from .models import AgentAbort

        raise AgentAbort(message)
