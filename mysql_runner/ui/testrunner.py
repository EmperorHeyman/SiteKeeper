"""Run a connection test off the GUI thread, and tidy up after it.

Two places test a connection - the Add/Edit dialog before anything is saved,
and the connection list afterwards - and both want the same three things: the
window stays responsive while a server that will never answer is dialled, the
answer arrives on the GUI thread, and the thread is disposed of safely even
when the window it belonged to is closing. That last one is the reason this
is a class rather than two calls: a connect in flight cannot be interrupted,
and a QThread destroyed while running aborts the process (ui/threadwatch.py).

The test itself is in connectiontest.py and knows nothing about Qt.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from mysql_runner import connectiontest
from mysql_runner.transfer import hostkeys
from mysql_runner.ui import threadwatch


class _Worker(QObject):
    """The blocking half, living on the thread."""

    done = pyqtSignal(object)

    @pyqtSlot(object, object)
    def run(self, profile: object, jump: object) -> None:
        self.done.emit(
            connectiontest.run(
                profile,
                jump=jump,
                # An SSH server nobody has confirmed comes back as a question
                # rather than being recorded. Trusting a key because someone
                # pressed Test would be the one check SSH has, skipped.
                host_key_mode=hostkeys.PROMPT,
            )
        )


class TestRunner(QObject):
    """One test at a time, with its thread owned and disposed of here."""

    #: The TestResult, on the GUI thread.
    finished = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, profile, jump: object = None) -> bool:
        """Begin a test. False when one is already running."""
        if self._thread is not None:
            return False
        self._thread = QThread(self)
        worker = self._worker = _Worker()
        worker.moveToThread(self._thread)
        worker.done.connect(self._on_done)
        # Started by the thread itself rather than by a queued signal, so
        # there is one thing to tear down and no request left in a queue.
        #
        # The worker is captured here rather than read from self when the
        # signal arrives. Press Test and close the window before the thread
        # has got going and stop() has already cleared the attribute, so a
        # lambda reaching for self._worker would raise inside the thread -
        # and an exception in a slot is fatal to the process, which is a
        # crash on a perfectly ordinary pair of keystrokes.
        self._thread.started.connect(lambda: worker.run(profile, jump))
        self._thread.start()
        return True

    def _on_done(self, result: object) -> None:
        self.stop()
        self.finished.emit(result)

    def stop(self) -> None:
        """Let go of the thread, retiring it if it will not stop in time."""
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        if thread is None:
            return
        try:
            worker.disconnect()
        except (TypeError, RuntimeError):
            pass
        thread.quit()
        if not thread.wait(2000):
            threadwatch.retire(thread, worker)
