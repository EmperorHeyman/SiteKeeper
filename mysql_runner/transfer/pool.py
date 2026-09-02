"""Multi-threaded transfer engine: several files at once, with a real queue.

One connection copying one file at a time wastes most of a link's capacity,
because each small file costs a round trip that nothing overlaps. This pool
opens a handful of *separate* connections and runs a job on each, which is the
difference between minutes and seconds on a tree of small files.

Five properties matter as much as the speed:

* **The navigation connection is never used for transfers.** Browsing stays
  responsive while a queue runs, because the pool's connections are its own.
* **The queue is controllable.** Items can be paused, resumed, reordered and
  cancelled individually, mid-file, not just between files.
* **Overwrites are safe.** With atomic uploads on, bytes go to a scratch name
  and are renamed into place, so a half-written file is never live; with shadow
  backups on, whatever was there is kept first so it can be put back.
* **A momentary failure is not a failed file.** Each file gets a few spaced-out
  attempts before it is given up on, and only for errors that could plausibly
  come out differently - see _carry and PERMANENT_FAILURES.
* **A retry does not start again.** Where the protocol allows it, an attempt
  picks up from what the last one left, so a 400 MB file that broke at 380 MB
  finishes in seconds rather than repeating twenty minutes of work.

There is also one thing the pool deliberately gives up: a single speed limit
across every connection, so somebody sharing an office link can have it back.

Everything here is Qt-free: progress arrives through plain callables, which the
Qt tab turns into signals and the FastAPI backend turns into WebSocket events.
Callbacks run on worker threads - whatever receives them must be thread-safe.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from mysql_runner.transfer.base import (
    Capability,
    ProgressCallback,
    RemoteFS,
    TransferError,
    local_relative,
    relative_posix,
    temp_name,
)
from mysql_runner.transfer.history import Action, HistoryStore, make_entry
from mysql_runner.transfer.ignore import IgnoreRules
from mysql_runner.transfer.throttle import RateLimiter

#: Priorities. Lower runs sooner; the queue is FIFO within one priority.
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 5
PRIORITY_LOW = 9

#: Default number of parallel connections.
#:
#: A deploy is not one big file, it is a thousand small ones, and each one
#: costs several round trips on top of its bytes - open, write, close, rename,
#: timestamp. On a link with any real latency that is what the time goes on:
#: measured over a 100 Mbit/s link with a 40 ms round trip, 150 files of 24 KB
#: carry 0.3 seconds of data and took 12.8 seconds on three connections. The
#: cost is latency, not bandwidth, so it divides almost exactly by the number
#: of connections - the same files took 6.6 seconds on six.
#:
#: Six is where politeness stops it. Shared hosting commonly caps a single
#: account at somewhere between four and ten simultaneous sessions, and the
#: pool now discovers that cap instead of failing transfers into it (see
#: _note_ceiling), so the number above the cap costs nothing but is not free
#: to assume either.
DEFAULT_WORKERS = 6
MAX_WORKERS = 16

#: Progress is reported at most this often per file, to keep the UI cheap.
PROGRESS_INTERVAL = 0.1

#: How long to wait before each retry of a failed file, in seconds. The first
#: is short because most of what it catches is momentary - a server briefly at
#: its session limit, a lock held by the request that was serving the page as
#: the file went over it - and the last is long enough to be worth trying.
RETRY_BACKOFF = (1.0, 4.0, 10.0)

#: Default number of retries per file, on top of the first attempt.
DEFAULT_RETRIES = 2

#: Errors that will say exactly the same thing however many times they are
#: asked. Retrying these costs the user seconds per file across a whole queue
#: and cannot possibly help, so a message containing one of these (the server's
#: own words, lower-cased) fails at once - everything else gets its retries.
PERMANENT_FAILURES = (
    "no such file",
    "not found",
    "permission denied",
    "access denied",
    "not a directory",
    "is a directory",
    "file exists",
    "no space left",
    "quota exceeded",
    "disk full",
    "read-only file system",
)

#: How often the aggregate transfer rate is re-measured, in seconds.
RATE_SAMPLE_INTERVAL = 0.6

#: Weight kept from the previous rate sample. A queue of small files is bursty
#: enough that the raw number jumps between "instant" and "nothing"; smoothing
#: is what turns it into a figure worth putting an estimate on.
RATE_SMOOTHING = 0.7


class JobState(str, Enum):
    """Where one queued file has got to."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED, JobState.SKIPPED)


class Overwrite(str, Enum):
    """What to do when the destination already has the file."""

    ALWAYS = "always"
    NEWER = "newer"   # Only when the source is newer or a different size.
    NEVER = "never"


@dataclass
class TransferItem:
    """One file in the queue."""

    upload: bool
    local: str
    remote: str
    size: int = 0
    priority: int = PRIORITY_NORMAL
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: JobState = JobState.QUEUED
    transferred: int = 0
    error: str = ""
    started: float | None = None
    finished_at: float | None = None
    #: True when the transfer replaced an existing file.
    replaced: bool = False
    #: Set when the item was skipped or backed up, for the status line.
    note: str = ""
    #: How many times this file has been tried, including the attempt running.
    attempts: int = 0
    #: Bytes already in place when the current attempt started, so a resumed
    #: transfer's percentage carries on rather than starting again at nought.
    resumed_from: int = 0
    _sequence: int = 0
    _cancel: bool = False

    @property
    def name(self) -> str:
        return os.path.basename(self.local) or self.remote.rsplit("/", 1)[-1]

    @property
    def source(self) -> str:
        return self.local if self.upload else self.remote

    @property
    def destination(self) -> str:
        return self.remote if self.upload else self.local

    @property
    def fraction(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(1.0, self.transferred / self.size)

    @property
    def rate(self) -> float:
        """Bytes per second so far, 0 when it has not started.

        Only the bytes this attempt actually moved: a file resumed at 380 of
        400 MB would otherwise report the whole 380 as though it had arrived
        in the second since the retry started.
        """
        if not self.started:
            return 0.0
        elapsed = (self.finished_at or time.monotonic()) - self.started
        moved = max(0, self.transferred - self.resumed_from)
        return moved / elapsed if elapsed > 0 else 0.0

    def snapshot(self) -> "TransferItem":
        """A detached copy, safe to hand to the GUI thread."""
        clone = TransferItem(
            upload=self.upload,
            local=self.local,
            remote=self.remote,
            size=self.size,
            priority=self.priority,
            id=self.id,
        )
        clone.state = self.state
        clone.transferred = self.transferred
        clone.error = self.error
        clone.started = self.started
        clone.finished_at = self.finished_at
        clone.replaced = self.replaced
        clone.note = self.note
        clone.attempts = self.attempts
        clone.resumed_from = self.resumed_from
        clone._sequence = self._sequence
        return clone


@dataclass
class PoolOptions:
    """How the pool behaves. All of it is user-visible in Settings."""

    workers: int = DEFAULT_WORKERS
    #: Upload to a scratch name and rename into place.
    atomic: bool = True
    #: Keep the previous version of anything overwritten.
    keep_backups: bool = True
    overwrite: Overwrite = Overwrite.ALWAYS
    #: Re-read the file after uploading and compare digests.
    verify: bool = False
    #: Give the uploaded copy the local file's modified time. One extra round
    #: trip per file, which on a tree of small files is a seventh of the
    #: deploy. It used to earn that back by making timestamp comparisons
    #: possible; now that syncs compare content it buys only honest-looking
    #: dates on the server, so it is worth being able to decline.
    preserve_times: bool = True
    #: How many times a failed file is tried again before it is given up on.
    #: Most transfer failures in the wild are momentary, and the alternative
    #: to retrying them is a queue that ends in a scatter of red rows somebody
    #: has to notice and press Retry on by hand.
    retries: int = DEFAULT_RETRIES
    #: Carry on where an interrupted transfer stopped rather than sending or
    #: fetching the whole file again. Only used where the protocol allows it -
    #: see RemoteFS.supports_resume.
    resume: bool = True
    #: Ceiling on the *combined* speed of every connection in the pool, in
    #: bytes per second. Zero is no limit, which is the default: the number
    #: matters to somebody deploying from a shared office link in the middle
    #: of the afternoon, and to nobody else.
    rate_limit: int = 0

    def sane(self) -> "PoolOptions":
        self.workers = max(1, min(MAX_WORKERS, int(self.workers or 1)))
        self.retries = max(0, min(len(RETRY_BACKOFF), int(self.retries or 0)))
        self.rate_limit = max(0, int(self.rate_limit or 0))
        return self


@dataclass
class PoolEvents:
    """Callbacks into whatever is showing the queue. All optional."""

    on_item: Callable[[TransferItem], None] | None = None
    on_progress: Callable[[TransferItem], None] | None = None
    on_message: Callable[[str], None] | None = None
    on_idle: Callable[[dict], None] | None = None

    def item(self, item: TransferItem) -> None:
        if self.on_item is not None:
            self.on_item(item.snapshot())

    def progress(self, item: TransferItem) -> None:
        if self.on_progress is not None:
            self.on_progress(item.snapshot())

    def message(self, text: str) -> None:
        if self.on_message is not None:
            self.on_message(text)

    def idle(self, stats: dict) -> None:
        if self.on_idle is not None:
            self.on_idle(stats)


class _Cancelled(Exception):
    """Raised inside a progress callback to abandon the file being copied."""


class TransferPool:
    """A queue of files and the connections that carry them."""

    def __init__(
        self,
        factory: Callable[[], RemoteFS],
        *,
        options: PoolOptions | None = None,
        events: PoolEvents | None = None,
        history: HistoryStore | None = None,
        profile_id: str = "",
        profile_label: str = "",
    ) -> None:
        self._factory = factory
        self._options = (options or PoolOptions()).sane()
        self._events = events or PoolEvents()
        self._history = history
        self._profile_id = profile_id
        self._profile_label = profile_label

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._items: list[TransferItem] = []
        self._sequence = 0
        self._paused = False
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._connections: dict[int, RemoteFS] = {}
        self._active = 0
        # Worker ids never repeat: a retired worker's slot must not be handed
        # to a new thread while another thread is still using that connection.
        self._next_worker = 0
        # How many connections this server turned out to allow. Lowered the
        # first time one is refused while others are working; never raised
        # again on this pool, so the discovery is paid for once.
        self._ceiling = MAX_WORKERS
        # Workers currently inside the factory. Opening a connection happens
        # outside the lock - it is a network round trip and must not hold
        # every other worker up - so "nobody is connected" is not the same
        # question as "nobody is getting connected", and a worker deciding
        # whether it is surplus has to ask the second one.
        self._connecting = 0
        # Why the last connection attempt failed, for the items that end up
        # with nothing able to carry them.
        self._connect_error = "Could not open a connection."
        # One budget for the whole pool, so the limit the user set is the
        # limit the link sees however many connections are open.
        self._limiter = RateLimiter(self._options.rate_limit)
        # Aggregate speed, measured across the queue rather than per file:
        # what somebody watching a deploy wants is how fast the *queue* is
        # going and when it will end, which no single file can answer.
        self._rate_bytes = 0
        self._rate_stamp = time.monotonic()
        self._rate_ewma = 0.0


    # ----- queue ----------------------------------------------------------
    def submit(self, items: list[TransferItem]) -> list[str]:
        """Add files to the queue and make sure workers are running."""
        if not items:
            return []
        with self._condition:
            if self._active == 0 and self._queued_count() == 0:
                # A fresh run: the byte counter it measures against has just
                # been reset by clear_finished, so carrying the old sample
                # over would report one enormous negative second.
                self._reset_rate()
            for item in items:
                self._sequence += 1
                item._sequence = self._sequence
                self._items.append(item)
            self._condition.notify_all()
        for item in items:
            self._events.item(item)
        self._ensure_workers()
        return [item.id for item in items]

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._stopping:
                return
            wanted = min(
                self._options.workers, self._ceiling, max(1, self._queued_count())
            )
            while len(self._threads) < wanted:
                index = self._next_worker
                self._next_worker += 1
                thread = threading.Thread(
                    target=self._worker,
                    args=(index,),
                    name=f"transfer-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def _queued_count(self) -> int:
        return sum(1 for item in self._items if item.state == JobState.QUEUED)

    # ----- control --------------------------------------------------------
    def pause(self) -> None:
        with self._condition:
            self._paused = True
        self._events.message("Transfers paused.")

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()
        self._events.message("Transfers resumed.")
        self._ensure_workers()

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_workers(self, count: int) -> None:
        """Change the connection count; new workers start on the next submit."""
        with self._lock:
            self._options.workers = max(1, min(MAX_WORKERS, int(count)))
        self._ensure_workers()

    def set_rate_limit(self, rate: int) -> None:
        """Change the combined speed ceiling, in bytes per second (0 = none).

        Takes effect on the next chunk of whatever is already in flight, not
        on the next file: someone reaching for this in the middle of a large
        upload is asking for their link back now.
        """
        rate = max(0, int(rate or 0))
        with self._lock:
            self._options.rate_limit = rate
        self._limiter.set_rate(rate)

    def update_options(self, options: PoolOptions) -> None:
        """Apply changed settings to a pool that is already running."""
        options = options.sane()
        with self._lock:
            self._options = options
        self._limiter.set_rate(options.rate_limit)
        self._ensure_workers()

    def cancel(self, item_id: str) -> bool:
        """Cancel one item, mid-file if it is already running."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state.finished:
                return False
            if item.state == JobState.QUEUED:
                item.state = JobState.CANCELLED
                item.finished_at = time.monotonic()
                self._events.item(item)
                return True
            item._cancel = True
            self._condition.notify_all()
        return True

    def cancel_all(self) -> int:
        """Cancel everything queued and running."""
        cancelled: list[TransferItem] = []
        with self._condition:
            for item in self._items:
                if item.state == JobState.QUEUED:
                    item.state = JobState.CANCELLED
                    item.finished_at = time.monotonic()
                    cancelled.append(item)
                elif item.state == JobState.RUNNING:
                    item._cancel = True
            self._paused = False
            self._condition.notify_all()
        for item in cancelled:
            self._events.item(item)
        return len(cancelled)

    def prioritize(self, item_id: str, *, priority: int = PRIORITY_HIGH) -> bool:
        """Move one queued item up (or down) the queue."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state != JobState.QUEUED:
                return False
            item.priority = priority
            if priority <= PRIORITY_HIGH:
                # Ahead of everything else already marked high.
                self._sequence += 1
                item._sequence = -self._sequence
            self._condition.notify_all()
        self._events.item(item)
        return True

    def reorder(self, item_ids: list[str]) -> None:
        """Apply an explicit order, as produced by dragging rows around."""
        with self._condition:
            positions = {item_id: index for index, item_id in enumerate(item_ids)}
            for item in self._items:
                if item.id in positions and item.state == JobState.QUEUED:
                    item.priority = PRIORITY_NORMAL
                    item._sequence = positions[item.id]
            self._condition.notify_all()

    def clear_finished(self, *, keep_failed: bool = False) -> int:
        """Forget finished items. ``keep_failed`` leaves failures retryable."""
        with self._condition:
            before = len(self._items)
            keeping: list[TransferItem] = []
            dropping: list[TransferItem] = []
            for item in self._items:
                if not item.state.finished or (
                    keep_failed and item.state == JobState.FAILED
                ):
                    keeping.append(item)
                else:
                    dropping.append(item)
            self._items = keeping
        # Forgetting a failed download means forgetting that the part of it
        # already on disk was worth anything, so the part goes too. Only local
        # scratch files can be here: the remote ones are removed the moment an
        # upload is given up on (see _carry).
        for item in dropping:
            self._discard_local_scratch(item)
        return before - len(self._items)

    def retry(self, item_id: str) -> bool:
        """Put one failed (or cancelled) item back in the queue."""
        with self._condition:
            item = self._find(item_id)
            if item is None or item.state not in (
                JobState.FAILED, JobState.CANCELLED
            ):
                return False
            item.state = JobState.QUEUED
            item.error = ""
            item.note = ""
            item.transferred = 0
            item.started = None
            item.finished_at = None
            item._cancel = False
            self._sequence += 1
            item._sequence = self._sequence
            self._condition.notify_all()
        self._events.item(item)
        self._ensure_workers()
        return True

    def retry_failed(self) -> int:
        """Queue every failed item again. Returns how many were requeued."""
        with self._lock:
            wanted = [
                item.id for item in self._items if item.state == JobState.FAILED
            ]
        return sum(1 for item_id in wanted if self.retry(item_id))

    def shutdown(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop the workers and close their connections."""
        with self._condition:
            self._stopping = True
            self._paused = False
            for item in self._items:
                if item.state == JobState.RUNNING:
                    item._cancel = True
            self._condition.notify_all()
        if wait:
            # Copy first: a worker retiring on its own removes itself from this
            # list while we are walking it.
            for thread in self._threads.copy():
                thread.join(timeout)
        with self._lock:
            connections = self._connections.copy()
            self._connections.clear()
            self._threads.clear()
        for connection in connections.values():
            try:
                connection.close()
            except Exception:
                pass

    # ----- inspection -----------------------------------------------------
    def items(self) -> list[TransferItem]:
        with self._lock:
            return [item.snapshot() for item in self._items]

    def stats(self) -> dict:
        with self._lock:
            tally = {state.value: 0 for state in JobState}
            done_bytes = 0
            total_bytes = 0
            for item in self._items:
                tally[item.state.value] += 1
                total_bytes += item.size
                done_bytes += item.size if item.state == JobState.DONE else item.transferred
            rate = self._sample_rate(done_bytes)
            remaining = max(0, total_bytes - done_bytes)
            # No estimate rather than a wrong one: a paused queue is not slow,
            # and a queue whose files are all of unknown size has nothing to
            # divide. Both used to show as "0 seconds left".
            eta = remaining / rate if rate > 0 and remaining and not self._paused else None
            return {
                "counts": tally,
                "bytes_done": done_bytes,
                "bytes_total": total_bytes,
                "rate": rate,
                "eta": eta,
                "paused": self._paused,
                "workers": self._options.workers,
                "active": self._active,
            }

    def _sample_rate(self, done_bytes: int) -> float:
        """Bytes per second across the queue, smoothed. Call with the lock held.

        Measured against the clock rather than summed from each running file's
        own average, because those averages are of different ages: a file that
        started a second ago and one that is nearly finished say very different
        things about a link that has not changed at all.
        """
        now = time.monotonic()
        elapsed = now - self._rate_stamp
        if elapsed < RATE_SAMPLE_INTERVAL:
            return self._rate_ewma
        # clear_finished can take completed bytes back out of the total, so a
        # sample is only ever of forward movement.
        sample = max(0, done_bytes - self._rate_bytes) / elapsed
        self._rate_bytes = done_bytes
        self._rate_stamp = now
        self._rate_ewma = (
            sample
            if self._rate_ewma <= 0
            else RATE_SMOOTHING * self._rate_ewma + (1 - RATE_SMOOTHING) * sample
        )
        return self._rate_ewma

    def _reset_rate(self) -> None:
        """Start measuring again. Call with the lock held."""
        self._rate_bytes = 0
        self._rate_stamp = time.monotonic()
        self._rate_ewma = 0.0

    def idle(self) -> bool:
        with self._lock:
            return self._active == 0 and self._queued_count() == 0

    def _find(self, item_id: str) -> TransferItem | None:
        return next((item for item in self._items if item.id == item_id), None)

    # ----- the worker loop ------------------------------------------------
    def _worker(self, index: int) -> None:
        """One connection, serving the queue until it is empty.

        The connection is opened *before* an item is claimed, and that order
        matters more than it looks. Claiming first meant a worker still dialling
        was holding a file nobody else could take: ask a server that allows
        three sessions for sixteen, and thirteen files were held hostage by
        workers waiting on a handshake, while the three that were connected saw
        an empty queue, retired, and closed the only working connections. The
        thirteen then woke up with nothing left to hand their files back to and
        failed them. A worker that cannot connect now simply retires holding
        nothing, which is the whole of what "one connection too many" should
        mean.
        """
        try:
            try:
                connection = self._connection_for(index)
            except TransferError as exc:
                self._note_ceiling(exc, index)
                return
            while True:
                item = self._next_item()
                if item is None:
                    return
                connection = self._carry(connection, index, item)
                if connection is None:
                    return  # nothing left to carry anything on
        finally:
            self._retire()
            self._close_connection(index)
            # Every way out of this loop ends here, which is the only place
            # that can tell whether the last worker has just gone.
            self._abandon_queue()

    def _carry(self, connection: RemoteFS, index: int, item: TransferItem):
        """Run one item to a conclusion, retrying what is worth retrying.

        Returns the connection to use for the next item - not always the one
        it was given, since a dropped session is replaced here - or None when
        this worker can no longer serve the queue at all.

        Two different failures used to land in the same place. A session that
        died while the pool sat idle fails whatever it is handed next, and that
        one has always earned a fresh connection and another go. But a live
        connection refusing a single file - a lock held by the request that was
        serving the page as it went over, a server momentarily at its session
        limit, a write that lost a race with a cron job - failed the file
        outright, and a queue of four hundred ended in a scatter of red rows
        that all succeeded the moment somebody pressed Retry. Both are now the
        same thing: a few attempts, spaced out, and only for errors that could
        plausibly come out differently.
        """
        attempts = max(0, int(self._options.retries)) + 1
        for attempt in range(1, attempts + 1):
            item.attempts = attempt
            if attempt > 1:
                item.note = ""  # drop the previous attempt's "trying again"
            error = self._run_item(connection, item)
            if error is None:
                return connection
            if self._stopping or item._cancel:
                self._fail(connection, item, error)
                return connection
            dropped = not connection.alive()
            if attempt >= attempts or (not dropped and not _worth_retrying(error)):
                self._fail(connection, item, error)
                return connection
            if dropped:
                self._close_connection(index)
                try:
                    connection = self._connection_for(index)
                except TransferError as exc:
                    self._fail(connection, item, str(exc))
                    self._events.message(f"Connection {index + 1}: {exc}")
                    return None
                self._events.message(
                    "A transfer connection was dropped; reconnected."
                )
            elif not self._wait_to_retry(attempt, item):
                self._fail(connection, item, error)
                return connection
            item.note = f"{error} - trying again ({attempt} of {attempts - 1})"
            self._events.item(item)
        return connection

    def _fail(self, fs: RemoteFS, item: TransferItem, error: str) -> None:
        """Give up on an item, and clean up after the attempts it had.

        The remote scratch of a half-finished upload always goes: leaving one
        behind so a later manual Retry could resume it would mean every upload
        that ever failed leaves a stray ``.mrtmp-`` file on somebody's server,
        which is a worse thing to be true than re-sending a file. A download's
        local part is kept - it costs nothing, it is on this machine, and it is
        what makes Retry on a 400 MB file finish in seconds.
        """
        self._discard_remote_scratch(fs, item)
        if item._cancel or self._stopping:
            self._discard_local_scratch(item)
            self._finish(item, JobState.CANCELLED)
            return
        self._finish(item, JobState.FAILED, error=error)

    def _wait_to_retry(self, attempt: int, item: TransferItem) -> bool:
        """Sleep before the next attempt. False when the wait was cut short."""
        delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)) - 1]
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self._stopping or item._cancel:
                return False
            time.sleep(0.1)
        return True

    def _note_ceiling(self, exc: TransferError, index: int) -> None:
        """Record what a refused connection tells us about this server.

        Refusing the surplus session is the server working correctly, so it is
        not reported as a failure while others are carrying files - it is a
        ceiling, remembered so the pool stops asking for more. With nothing
        connected and nothing connecting it is a real problem, and the queue is
        told on the way out (see _abandon_queue).
        """
        with self._condition:
            self._connect_error = str(exc) or "Could not open a connection."
            live = len(self._connections)
            working = live or self._connecting
            if not working:
                self._events.message(f"Connection {index + 1}: {exc}")
                return
            settled = max(1, min(self._ceiling, live or self._ceiling))
            announce = settled < self._ceiling
            self._ceiling = settled
            self._condition.notify_all()
        if announce:
            self._events.message(
                f"This server allows {settled} transfer connection(s) at once; "
                "using that many."
            )

    def _abandon_queue(self) -> None:
        """Fail whatever is still waiting when nothing is left to carry it.

        A worker that cannot open a connection retires. If it was the last one,
        every item still queued has nobody to transfer it - and a queue that
        never drains reports nothing, shows no failure, and simply sits there
        looking as though it is working. Better to say so; *Retry failed* is
        one click, and it is the honest state.
        """
        with self._condition:
            if self._threads or self._connections or self._connecting:
                return  # somebody is still working, connected, or connecting
            error = self._connect_error
            stranded = [
                item for item in self._items if item.state == JobState.QUEUED
            ]
            for item in stranded:
                item.state = JobState.FAILED
                item.error = error
                item.finished_at = time.monotonic()
            self._condition.notify_all()
        for item in stranded:
            self._events.item(item)
        if stranded:
            self._events.message(
                f"{len(stranded)} transfer(s) could not start: {error}"
            )
            self._events.idle(self.stats())

    def _retire(self) -> None:
        """Take this thread off the roster so a later submit can start a fresh one."""
        with self._lock:
            current = threading.current_thread()
            self._threads = [thread for thread in self._threads if thread is not current]

    def _next_item(self) -> TransferItem | None:
        with self._condition:
            while True:
                if self._stopping:
                    return None
                if not self._paused:
                    item = self._pick()
                    if item is not None:
                        item.state = JobState.RUNNING
                        item.started = time.monotonic()
                        self._active += 1
                        self._events.item(item)
                        return item
                    if self._queued_count() == 0:
                        return  # Nothing left to do; the thread retires. None
                self._condition.wait(0.25)

    def _pick(self) -> TransferItem | None:
        best: TransferItem | None = None
        for item in self._items:
            if item.state != JobState.QUEUED:
                continue
            if best is None or (item.priority, item._sequence) < (best.priority, best._sequence):
                best = item
        return best

    def _connection_for(self, index: int) -> RemoteFS:
        with self._lock:
            existing = self._connections.get(index)
            if existing is not None:
                return existing
            self._connecting += 1
        # The count is only given up once the connection is *visible* to
        # everyone else, not merely once the factory has returned. Dropping it
        # first leaves a window in which a worker that has succeeded is
        # neither connecting nor connected, and another worker checking in
        # exactly that window concludes the server is unreachable and fails a
        # perfectly good transfer.
        try:
            connection = self._factory()
        except BaseException as exc:
            with self._lock:
                self._connecting -= 1
                if isinstance(exc, TransferError):
                    self._connect_error = (
                        str(exc) or "Could not open a connection."
                    )
            raise
        with self._lock:
            self._connections[index] = connection
            self._connecting -= 1
        return connection

    def _close_connection(self, index: int) -> None:
        with self._lock:
            connection = self._connections.pop(index, None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    # ----- one file -------------------------------------------------------
    def _run_item(self, fs: RemoteFS, item: TransferItem) -> str | None:
        """Carry one file. A remote failure is *returned*, not finished, so
        the worker loop can decide whether a fresh connection deserves a retry;
        every other outcome (done, skipped, cancelled, local error) is final.
        """
        try:
            if self._should_skip(fs, item):
                self._finish(item, JobState.SKIPPED)
                return None
            self._prepare_destination(item)
            if item.attempts <= 1:
                # Once per file, not once per attempt: the backup is a whole
                # download of the old remote copy, and repeating it on a retry
                # would cost more than the transfer being retried - and write a
                # second journal entry for one replacement.
                self._backup_destination(fs, item)
            if item.upload:
                self._do_upload(fs, item)
            else:
                self._do_download(fs, item)
        except _Cancelled:
            self._discard_remote_scratch(fs, item)
            self._discard_local_scratch(item)
            self._finish(item, JobState.CANCELLED)
            return None
        except TransferError as exc:
            return str(exc) or exc.__class__.__name__
        except OSError as exc:
            self._finish(item, JobState.FAILED, error=str(exc))
            return None
        self._finish(item, JobState.DONE)
        return None

    def _should_skip(self, fs: RemoteFS, item: TransferItem) -> bool:
        mode = self._options.overwrite
        if mode == Overwrite.ALWAYS:
            return False
        target_exists, target_size, target_mtime = self._destination_facts(fs, item)
        if not target_exists:
            return False
        item.replaced = True
        if mode == Overwrite.NEVER:
            item.note = "kept the existing file"
            return True
        source_size, source_mtime = self._source_facts(fs, item)
        if source_size != target_size:
            return False
        if source_mtime is None or target_mtime is None:
            return False
        if source_mtime <= target_mtime + 2.0:
            item.note = "already up to date"
            return True
        return False

    def _destination_facts(self, fs: RemoteFS, item: TransferItem):
        if item.upload:
            try:
                stat = fs.stat(item.remote)
            except TransferError:
                return False, 0, None
            return True, stat.size, stat.modified
        try:
            result = os.stat(item.local)
        except OSError:
            return False, 0, None
        return True, result.st_size, result.st_mtime

    def _source_facts(self, fs: RemoteFS, item: TransferItem):
        if item.upload:
            try:
                result = os.stat(item.local)
            except OSError:
                return item.size, None
            return result.st_size, result.st_mtime
        try:
            stat = fs.stat(item.remote)
        except TransferError:
            return item.size, None
        return stat.size, stat.modified

    def _prepare_destination(self, item: TransferItem) -> None:
        """Make sure a download has somewhere to land."""
        if item.upload:
            return
        parent = os.path.dirname(item.local)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _backup_destination(self, fs: RemoteFS, item: TransferItem) -> None:
        """Keep whatever is about to be overwritten, if that is switched on."""
        if self._history is None or not self._options.keep_backups:
            return
        if item.upload:
            try:
                existing = fs.stat(item.remote)
            except TransferError:
                return
            item.replaced = True
            if self._unchanged(item.local, existing):
                # Replacing a file with an identical copy loses nothing, so
                # the backup - a full download of the old file - is skipped.
                # Re-deploys are mostly unchanged files; this is where the
                # bulk of their time used to go.
                return
            backup, size, note = self._history.keep_remote_copy(
                fs, item.remote, size=existing.size
            )
            action = Action.REPLACED_REMOTE
            target = item.remote
        else:
            if not os.path.isfile(item.local):
                return
            item.replaced = True
            backup, size, note = self._history.keep_local_copy(item.local)
            action = Action.REPLACED_LOCAL
            target = item.local
        if note:
            item.note = note
        try:
            self._history.record(
                make_entry(
                    action,
                    profile_id=self._profile_id,
                    profile_label=self._profile_label,
                    target=target,
                    backup=backup,
                    size=size,
                    note=note,
                )
            )
        except OSError as exc:
            # The journal is bookkeeping about the transfer; failing to write
            # it must not fail the transfer itself.
            item.note = f"backup kept but not journalled ({exc})"

    @staticmethod
    def _unchanged(local: str, existing) -> bool:
        """Whether the server copy already matches the file about to go up.

        Uploads carry the local timestamp over (see _preserve_mtime), so same
        size and same mtime means the last upload of this very file - the two
        seconds of slack absorb filesystems that round timestamps.
        """
        try:
            result = os.stat(local)
        except OSError:
            return False
        if existing.modified is None:
            return False
        return (
            result.st_size == existing.size
            and abs(result.st_mtime - existing.modified) <= 2.0
        )

    # ----- scratch files and resuming -------------------------------------
    # Both scratch names are derived from the item's id rather than a fresh
    # uuid per attempt, which is the whole of what makes resuming possible: a
    # retry has to be able to find what the attempt before it left behind.
    @staticmethod
    def _scratch_remote(item: TransferItem) -> str:
        return temp_name(item.remote, item.id)

    @staticmethod
    def _scratch_local(item: TransferItem) -> str:
        return f"{item.local}.mrpart-{item.id}"

    def _can_resume(self, fs: RemoteFS) -> bool:
        return bool(self._options.resume) and fs.supports_resume()

    def _resume_offset(self, fs: RemoteFS, item: TransferItem) -> int:
        """How much of an upload is already on the server. 0 to start again.

        Never on the first attempt: a scratch file at that point can only be
        one this pool did not write - left by a crash, or by a build that used
        the same id scheme - and resuming into somebody else's bytes would
        produce a file that is the right length and wrong all the way through.
        """
        if item.attempts <= 1 or not self._can_resume(fs):
            return 0
        try:
            existing = int(fs.stat(self._scratch_remote(item)).size or 0)
        except TransferError:
            return 0
        try:
            total = os.path.getsize(item.local)
        except OSError:
            return 0
        return existing if 0 < existing <= total else 0

    def _local_resume_offset(self, fs: RemoteFS, item: TransferItem) -> int:
        """How much of a download is already on disk. 0 to start again."""
        if item.attempts <= 1 or not self._can_resume(fs):
            return 0
        try:
            have = os.path.getsize(self._scratch_local(item))
        except OSError:
            return 0
        if have <= 0 or (item.size and have > item.size):
            return 0  # nothing, or more than the source has: not this file
        return have

    def _discard_remote_scratch(self, fs: RemoteFS, item: TransferItem) -> None:
        if not item.upload:
            return
        try:
            fs.remove(self._scratch_remote(item))
        except Exception:
            pass  # never created, already gone, or the session is dead

    def _discard_local_scratch(self, item: TransferItem) -> None:
        if item.upload:
            return
        try:
            os.unlink(self._scratch_local(item))
        except OSError:
            pass

    def _do_upload(self, fs: RemoteFS, item: TransferItem) -> None:
        if not self._options.atomic:
            # Straight at the destination: there is no scratch to resume into,
            # and appending to a file a live request may be reading is the very
            # thing atomic uploads exist to prevent.
            item.resumed_from = 0
            fs.upload(item.local, item.remote, self._progress_for(item))
            self._verify(fs, item)
            return
        scratch = self._scratch_remote(item)
        item.resumed_from = self._resume_offset(fs, item)
        item.note = _resume_note(item)
        progress = self._progress_for(item)
        # A TransferError deliberately leaves the scratch where it is: the next
        # attempt resumes into it, and _fail removes it when there is no next
        # attempt. A rename that failed on a complete scratch is the happiest
        # case of all - the retry re-sends nothing and just renames again.
        fs.upload(item.local, scratch, progress, resume_from=item.resumed_from)
        fs.replace(scratch, item.remote)
        self._preserve_mtime(fs, item)
        self._verify(fs, item)

    def _do_download(self, fs: RemoteFS, item: TransferItem) -> None:
        if not self._options.atomic:
            item.resumed_from = 0
            fs.download(item.remote, item.local, self._progress_for(item))
            return
        scratch = self._scratch_local(item)
        item.resumed_from = self._local_resume_offset(fs, item)
        item.note = _resume_note(item)
        progress = self._progress_for(item)
        keep = self._can_resume(fs)
        try:
            fs.download(
                item.remote,
                scratch,
                progress,
                resume_from=item.resumed_from,
                keep_partial=keep,
            )
            os.replace(scratch, item.local)
        except (TransferError, OSError):
            if not keep:
                self._discard_local_scratch(item)
            raise

    def _preserve_mtime(self, fs: RemoteFS, item: TransferItem) -> None:
        """Give the uploaded copy the local file's timestamp, when possible.

        This used to be load-bearing: without it every deploy looked like it
        changed every file, and the sync comparison was by timestamp. The
        comparison is by content now, so what this buys is dates on the server
        that match the dates on your machine - worth having, worth a round trip
        per file, and worth being able to turn off when a deploy of ten
        thousand small files is waiting on exactly that round trip.
        """
        if not self._options.preserve_times:
            return
        if not fs.supports(Capability.SET_MTIME):
            return
        try:
            stamp = os.path.getmtime(item.local)
        except OSError:
            return
        try:
            fs.set_mtime(item.remote, stamp)
        except TransferError:
            pass  # Cosmetic; never fail a transfer over it.

    def _verify(self, fs: RemoteFS, item: TransferItem) -> None:
        if not self._options.verify:
            return
        from mysql_runner.transfer.hashing import hash_local_file, hash_remote_file

        local_digest = hash_local_file(item.local)
        remote_digest = hash_remote_file(fs, item.remote)
        if remote_digest and local_digest != remote_digest:
            raise TransferError(
                f"{item.name} does not match after uploading - the copy on the "
                "server differs from the local file."
            )
        item.note = "verified"

    def _progress_for(self, item: TransferItem) -> ProgressCallback:
        # "seen" starts at the resume mark, so a resumed transfer's first
        # callback is not charged to the speed limit for bytes that went over
        # the wire yesterday.
        state = {"last": 0.0, "seen": float(item.resumed_from)}

        def interrupt() -> None:
            """Let a cancel out of a long wait for the speed limit."""
            if item._cancel or self._stopping:
                raise _Cancelled()

        def report(transferred: int, total: int) -> None:
            # This runs once per 32 KB on every worker at once, so it reads
            # _paused directly rather than through the locked property: taking
            # the pool's lock thousands of times a second makes the workers
            # queue up behind each other for a flag that is only ever a bool.
            # A read that is one chunk stale is harmless - the pause takes
            # effect 32 KB later.
            if item._cancel or self._stopping:
                raise _Cancelled()
            while self._paused:
                if item._cancel or self._stopping:
                    raise _Cancelled()
                time.sleep(0.1)
            # The bytes are already on the wire by the time a backend calls
            # back, so the budget is paid afterwards and it is the *next*
            # chunk that waits. One bucket for the whole pool, so the limit
            # does not multiply by the number of connections.
            moved = transferred - state["seen"]
            if moved > 0:
                state["seen"] = transferred
                self._limiter.take(int(moved), interrupt)
            item.transferred = transferred
            if total and total != item.size:
                item.size = total
            now = time.monotonic()
            if now - state["last"] >= PROGRESS_INTERVAL:
                state["last"] = now
                self._events.progress(item)

        return report

    def _finish(self, item: TransferItem, state: JobState, *, error: str = "") -> None:
        with self._condition:
            item.state = state
            item.error = error
            item.finished_at = time.monotonic()
            if state == JobState.DONE and item.size:
                item.transferred = item.size
            if self._active > 0:
                self._active -= 1
            self._condition.notify_all()
            was_idle = self._active == 0 and self._queued_count() == 0
        self._events.item(item)
        if was_idle:
            self._events.idle(self.stats())


def _resume_note(item: TransferItem) -> str:
    """What the queue row should say about a transfer picking up mid-file."""
    if item.resumed_from <= 0:
        return ""
    if item.size:
        percent = int(item.resumed_from * 100 / item.size)
        return f"resuming at {percent}%"
    return "resuming"


def _worth_retrying(error: str) -> bool:
    """Whether an error could plausibly come out differently next time.

    The point is not to be clever about it - it is to avoid spending fifteen
    seconds per file learning that a permission has not changed. Anything not
    recognised as settled gets its retries, because the cost of retrying
    something hopeless is seconds and the cost of not retrying something
    momentary is a failed deploy.
    """
    text = (error or "").lower()
    return not any(phrase in text for phrase in PERMANENT_FAILURES)


# ----- building a queue ---------------------------------------------------
def expand_local(
    fs: RemoteFS,
    sources: list[tuple[str, bool]],
    remote_base: str,
    *,
    rules: IgnoreRules | None = None,
    priority: int = PRIORITY_NORMAL,
) -> tuple[list[TransferItem], list[str], list[str]]:
    """Walk local selections into upload items.

    Returns (items, remote directories to create, skipped paths).
    """
    rules = rules or IgnoreRules.empty()
    items: list[TransferItem] = []
    directories: list[str] = []
    skipped: list[str] = []
    for path, is_dir in sources:
        name = os.path.basename(path.rstrip("\\/")) or path
        if rules.is_ignored(name, is_dir=is_dir):
            skipped.append(path)
            continue
        if not is_dir:
            items.append(
                TransferItem(
                    upload=True,
                    local=path,
                    remote=fs.join(remote_base, name),
                    size=_local_size(path),
                    priority=priority,
                )
            )
            continue
        target = fs.join(remote_base, name)
        directories.append(target)
        _walk_local_dir(fs, path, target, rules, items, directories, skipped, priority)
    return items, directories, skipped


def _walk_local_dir(
    fs: RemoteFS,
    root: str,
    remote_root: str,
    rules: IgnoreRules,
    items: list[TransferItem],
    directories: list[str],
    skipped: list[str],
    priority: int,
) -> None:
    """Plan one local directory tree into upload items."""
    for current, dirnames, filenames in os.walk(root):
        rel = local_relative(root, current)
        rel = "" if rel == "." else rel
        keep: list[str] = []
        for name in sorted(dirnames):
            child_rel = f"{rel}/{name}" if rel else name
            full = os.path.join(current, name)
            if rules.is_ignored(child_rel, is_dir=True) or os.path.islink(full):
                skipped.append(full)
                continue
            keep.append(name)
            directories.append(fs.join(remote_root, child_rel))
        dirnames[:] = keep
        for name in sorted(filenames):
            child_rel = f"{rel}/{name}" if rel else name
            full = os.path.join(current, name)
            if rules.is_ignored(child_rel):
                skipped.append(full)
                continue
            items.append(
                TransferItem(
                    upload=True,
                    local=full,
                    remote=fs.join(remote_root, child_rel),
                    size=_local_size(full),
                    priority=priority,
                )
            )


def expand_remote(
    fs: RemoteFS,
    sources: list[tuple[str, bool]],
    local_base: str,
    *,
    rules: IgnoreRules | None = None,
    priority: int = PRIORITY_NORMAL,
) -> tuple[list[TransferItem], list[str], list[str]]:
    """Walk remote selections into download items.

    Returns (items, local directories to create, skipped paths). The walk uses
    ``fs`` - the navigation connection - so the pool's own connections stay
    free for the transfers themselves.
    """
    rules = rules or IgnoreRules.empty()
    items: list[TransferItem] = []
    directories: list[str] = []
    skipped: list[str] = []
    for path, is_dir in sources:
        name = fs.basename(path)
        if rules.is_ignored(name, is_dir=is_dir):
            skipped.append(path)
            continue
        if not is_dir:
            items.append(
                TransferItem(
                    upload=False,
                    local=os.path.join(local_base, name),
                    remote=path,
                    size=_remote_size(fs, path),
                    priority=priority,
                )
            )
            continue
        target = os.path.join(local_base, name)
        directories.append(target)
        _walk_remote_dir(fs, path, target, rules, items, directories, skipped, priority)
    return items, directories, skipped


def _walk_remote_dir(
    fs: RemoteFS,
    root: str,
    local_root: str,
    rules: IgnoreRules,
    items: list[TransferItem],
    directories: list[str],
    skipped: list[str],
    priority: int,
) -> None:
    """Plan one remote directory tree into download items."""
    for current, entries in fs.walk(root):
        rel = relative_posix(root, current)
        local_dir = os.path.join(local_root, rel.replace("/", os.sep)) if rel else local_root
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if rules.is_ignored(child_rel, is_dir=entry.is_dir):
                skipped.append(fs.join(current, entry.name))
                continue
            if entry.is_dir:
                if not entry.is_link:
                    directories.append(os.path.join(local_dir, entry.name))
                continue
            items.append(
                TransferItem(
                    upload=False,
                    local=os.path.join(local_dir, entry.name),
                    remote=fs.join(current, entry.name),
                    size=entry.size,
                    priority=priority,
                )
            )


def _local_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remote_size(fs: RemoteFS, path: str) -> int:
    try:
        return fs.stat(path).size
    except TransferError:
        return 0
