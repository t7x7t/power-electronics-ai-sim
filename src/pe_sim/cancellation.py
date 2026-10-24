"""Cooperative cancellation as a contract, not a test hook.

The runner asks one question at each control-window boundary::

    if token.cancelled():
        ...

That is the entire protocol.  The runner does not know *how* the request was
delivered, so the same run loop serves an in-process client, a file-watching
CLI invocation, or a future service without a kernel change.

Cancellation is deliberately distinct from an operator interrupt:

- ``cancelled`` means a client asked the run to stop.  The package is
  published as ``INCOMPLETE`` with failure category ``cancelled`` and a
  checkpoint when the adapter can snapshot.
- ``interrupted`` means the process received ``KeyboardInterrupt``.  The
  package is also ``INCOMPLETE`` but the failure category stays
  ``interrupted``, because the two causes have different owners.

Both are recoverable through ``resume_from``; neither is a backend failure.

The granularity is one control window.  An engine that cannot be stopped
inside a window therefore cannot promise instantaneous cancellation, and the
contract says so instead of pretending otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class CancelToken(Protocol):
    """The whole cancellation surface the runner depends on."""

    def cancelled(self) -> bool:
        """Return True when the caller wants the run to stop."""


class CallbackCancelToken:
    """Adapt a zero-argument predicate to the ``CancelToken`` protocol.

    The predicate is called at every window boundary, so it must be cheap and
    must not raise.  A raising predicate is a programming error in the client,
    not a run failure; the runner lets it surface rather than guessing a
    cancellation.
    """

    def __init__(self, predicate: Callable[[], object]):
        if not callable(predicate):
            raise TypeError("predicate must be callable")
        self._predicate = predicate

    def cancelled(self) -> bool:
        return bool(self._predicate())


class FileCancelToken:
    """Treat the existence of a path as the cancellation request.

    This is the delivery mechanism used by the CLI so an external client can
    cancel a run it did not start in-process.  The token only *reads* the
    path: it never creates, truncates, or deletes it, because the client owns
    the request and must be able to observe that its own signal was delivered.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def cancelled(self) -> bool:
        return self.path.exists()


__all__ = ["CancelToken", "CallbackCancelToken", "FileCancelToken"]
