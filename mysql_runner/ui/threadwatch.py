"""Let a worker thread outlive the tab that owned it, rather than crashing.

Every session tab runs its network work on a QThread it owns and stops in
``cleanup()``: quit the event loop, wait a few seconds, done. That works until
the worker is inside a call that cannot be interrupted - a driver dialling a
host that will never answer, which takes as long as its login timeout no
matter who is waiting. The wait times out, the tab is destroyed, and with it
the QThread it parented: Qt's answer to destroying a running QThread is to
abort the process. Closing a tab while it was connecting took the whole
application down with it, and the crash landed nowhere near the cause.

So a thread that will not stop in time is retired instead of destroyed: taken
out of the widget's ownership and held here, alive, until it finishes on its
own. It has nothing left to talk to - the tab disconnected its signals before
calling this - so it wakes up, finds its connection attempt refused, unwinds
and goes away. A handful of bytes for a few seconds, instead of a crash.
"""

from __future__ import annotations

#: Threads still unwinding, and the objects that were living on them. Held
#: only to keep Python from collecting either while the thread runs.
_retired: set[tuple] = set()


def retire(thread, *owned) -> bool:
    """Stop owning ``thread``. True when it had already finished.

    ``owned`` is whatever lived on that thread - the worker object, usually -
    which must not be collected while it is still running code.
    """
    if thread is None or not thread.isRunning():
        return True
    entry = (thread, owned)
    _retired.add(entry)
    # Both halves matter: setParent stops the C++ side being deleted with the
    # widget, and the set stops the Python side being collected.
    thread.setParent(None)
    thread.finished.connect(lambda: _retired.discard(entry))
    return False


def pending() -> int:
    """How many threads are still unwinding. For tests and diagnostics."""
    return len(_retired)
