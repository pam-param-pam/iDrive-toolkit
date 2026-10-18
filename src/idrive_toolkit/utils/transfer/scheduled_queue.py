"""A bounded transfer queue whose retries do not occupy sleeping workers."""
import heapq
import itertools
import time
from queue import Empty, Queue


class ScheduledQueue(Queue):
    def _init(self, maxsize):
        self.queue = []
        self._sequence = itertools.count()

    def _put(self, item):
        heapq.heappush(self.queue, (time.monotonic(), next(self._sequence), item))

    def _get(self):
        return heapq.heappop(self.queue)[2]

    def defer(self, item, delay):
        # A worker returns an owned request. Never block it behind new producer
        # work: that can deadlock every consumer when the bounded queue is full.
        with self.not_empty:
            heapq.heappush(self.queue, (time.monotonic() + max(0, delay), next(self._sequence), item))
            self.unfinished_tasks += 1
            self.not_empty.notify_all()

    def ready_count(self):
        with self.mutex:
            now = time.monotonic()
            return sum(due <= now for due, _, _ in self.queue)

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.not_empty:
            while True:
                now = time.monotonic()
                if self.queue and self.queue[0][0] <= now:
                    item = self._get()
                    self.not_full.notify()
                    return item
                if not block or (deadline is not None and now >= deadline):
                    raise Empty
                delay = None if not self.queue else max(0, self.queue[0][0] - now)
                if deadline is not None:
                    remaining = max(0, deadline - now)
                    delay = remaining if delay is None else min(delay, remaining)
                self.not_empty.wait(delay)

    def discard_pending(self):
        with self.mutex:
            self.unfinished_tasks -= len(self.queue)
            self.queue.clear()
            self.not_full.notify_all()
            if not self.unfinished_tasks:
                self.all_tasks_done.notify_all()
